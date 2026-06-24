# Expose API to make it easier to Suspend/resume tracking for allocations inside a particular thread at will
# Allows generating Flamegraph from (make-use) API , rather than routing to `subprocess` command.
#
# Issues:
# 1. For now maximum RSS usage would not be "correctly" reflected in the generated Flamegraph, as original CODE still assumes `process` level STATS !
# 2. To add some filtering either `index` based or `timestamp` based to collect stats for each independent `cycle` rather than accumulating, as there is still a single `.bin` file is assumed by original Memray code.
# 3. Speed up flamegraph generation by modifying `reader` code to just use the data from the Memory rather than round-trips to DISK!!
# 4. apply hook on `thread-exit` to reset the `Pid` stored in mapping, so as that space could be reused/freed for future use, since that particular Pid may not be returned/attributed for some time by OS (atleast in case of linux)!

from array import ArrayType, array
import threading
from threading import RLock
import os,random
import io

# It assumes, we are using our fork of memray.
import memray
from memray import FileReader, AllocationRecord, FileDestination, Tracker
from memray.reporters.flamegraph import FlameGraphReporter

# Globals.
n_max_threads_tracked = 32
py_to_cpp_mapping:ArrayType[int]
py_to_cpp_mapping_lock:RLock
py_to_cpp_mapping_payload = 1          # to hold the length of `array` in header. (1 int32)
memray_bin_fd:int = -1                     # underlying main `.bin` file file-descriptor!
f_python_side:io.FileIO

def init_memray_extended(max_threads:int = 64) -> Tracker:
  """Inputs:
  max_threads:int , no of maximum independent threads at 1 time to allow tracking for.
  Create a mapping/buffer to be interpreted by C++ memray
  Must be called before any of following function calls!
  """
  global py_to_cpp_mapping, py_to_cpp_mapping_lock, py_to_cpp_mapping_payload, n_max_threads_tracked, memray_bin_fd, f_python_side

  n_max_threads_tracked = max_threads
  py_to_cpp_mapping_payload  = 1  # 1 logical element used to store the SIZE Of array itself.
  py_to_cpp_mapping = array('i')  # int32
  assert py_to_cpp_mapping.itemsize == 4
  # expand x enough to hold a mapping from `process` id to the `FLAG` (suspend or resume)
  for i in range(max_threads):
    py_to_cpp_mapping.append(0) # place-holder for process/thread os native, not of python.
    py_to_cpp_mapping.append(0) # flag 0 = Suspend (default), flag = 1, Resume.
  py_to_cpp_mapping.append(0)   # This would be pass the `array` size info to the C++ runtime. (aka py_to_cpp_mapping_payload)
  # NOTE: DON'T UPDATE `x_mapping` from here on to prevent any resizing, hence violating the initial buffer_ptr
  (buffer_ptr, count) = py_to_cpp_mapping.buffer_info()
  assert count == max_threads * 2 + py_to_cpp_mapping_payload, f"count is {count}"
  assert buffer_ptr != 0
  py_to_cpp_mapping[0] = max_threads * 2  # store the (max) possible size of the `buffer` for the C++ runtime!

  # initialize lock
  py_to_cpp_mapping_lock = threading.RLock()

  bin_file_path = f"./memrary_{random.randint(1000, 2000)}_{random.randint(2000, 3000)}.bin"
  f_python_side = open(bin_file_path, "wb+")
  memray_bin_fd = f_python_side.fileno()

  fd_path = "/proc/self/fd/{}".format(memray_bin_fd)
  bin_destination = FileDestination(fd_path, overwrite = True)

  # create the (global) Tracker (it won't be activate just yet, only after on `__enter__` call!)
  tracker = memray.Tracker(
      buffer_ptr,
      destination = bin_destination,
      trace_python_allocators=False,
      follow_fork=False
  )
  print(f"[Info]: Tracker created . Writing records to: {bin_file_path}")
  return tracker


def _find_spot_in_mapping(thread_id:int)->int:
  # Returns the (absolute) index for corresponding `thread_id` in mapping.(just set next-index to 1/0 to indicate suspension and resumption!)
  spot_index = -1
  offset = py_to_cpp_mapping_payload
  for i in range(n_max_threads_tracked):
    id_index_to_check = 2*i + offset
    if py_to_cpp_mapping[id_index_to_check] == thread_id:  # do i have to cast it to int32 !! (seems to work atleast in python land)
      spot_index = id_index_to_check
      break
    elif py_to_cpp_mapping[id_index_to_check] == 0:
      spot_index = id_index_to_check
      break
  assert spot_index != -1, "Either empty or already assigned spot must have been found!"
  return spot_index

def resume_writing_allocations():
  _ = py_to_cpp_mapping_lock.acquire()
  # Resume writing allocations to underlying Common `.bin` file memray has created a single writer for!
  os_thread_id = threading.get_native_id()   # so each thread calling this would get the corresponding `os` process id, it would being executed in!
  spot_index = _find_spot_in_mapping(os_thread_id)
  py_to_cpp_mapping[spot_index] = os_thread_id  # even if already written, we would write the SAME value again, not big deallllll!
  assert py_to_cpp_mapping[spot_index + 1] == 0, "Why are you calling resume again... some bug or mis-assumption!!"
  py_to_cpp_mapping[spot_index + 1] = 1         # indicate resume.
  py_to_cpp_mapping_lock.release()

def suspend_writing_allocations(generate_flamegraph:bool = False):
  # Suspend writing allocations to underlying Common `.bin` file memray has created a single writer for!
  _ = py_to_cpp_mapping_lock.acquire()
  os_thread_id = threading.get_native_id()   # so each thread calling this would get the corresponding `os` process id, it would being executed in!
  spot_index = _find_spot_in_mapping(os_thread_id)
  py_to_cpp_mapping[spot_index] = os_thread_id  # even if already written, we would write the SAME value again, not big deallllll!
  assert py_to_cpp_mapping[spot_index + 1] == 1, "Why are you calling suspend again... some bug or mis-assumption!!"
  py_to_cpp_mapping[spot_index + 1] = 0         # indicate suspend
  if generate_flamegraph:
    _generate_flamegraph(os_thread_id)
  py_to_cpp_mapping_lock.release()

def _generate_flamegraph(os_thread_id:int):
  """Inputs:
    fd:int  file descriptor for the underlying main `.bin` file used by `memray`. (we can use it from python side too!)
  """
  # Supposed to be called by `suspend` after writing 0 to mapping, so as no more records would being written for thread which was suspended.
  # We use this to write corresponding flamegraph during a particular interval.
  global f_python_side, memray_bin_fd
  assert (memray_bin_fd > 0)

  # Copy the data available from main underlying .bin file into a BUFFER, so as to process it in isolation!
  os.fsync(memray_bin_fd)
  metadata = os.fstat(memray_bin_fd) # TODO: should we use `lseek` to get more correct estimate, as for Memory-Mapped file it is a bit different semantics.. TODO
  expected_size = metadata.st_size   # in bytes

  f_python_side.seek(0)                     # what effects could it have in case underlying FILE is a memory Mapped file?
  buffer = bytearray(expected_size)
  f_python_side.readinto(buffer)

  reader_file_path = f"./flamegraph_temp_{os_thread_id}_1.bin"
  with open(reader_file_path, "wb", buffering=0) as f_temp:
    f_temp.write(buffer)
    f_temp.flush()

  # create a dedicated reader for this `.bin` file, so that we can process it..
  reader = FileReader(file_name = reader_file_path)  # create a dedicated read
  print("[INFO]: Reader created!", flush = True)
  snapshot = list(reader.get_allocation_records()) # inside the code we have modified to pass exception raised..
  desired_records:list[AllocationRecord] = list()
  # TODO: filter it based on some recent index store or some timestamp!
  for r in reader.get_allocation_records():
    if r.tid == os_thread_id:
      desired_records.append(r)
  del snapshot
  memory_records = list(reader.get_memory_snapshots()) # pass proper iterarable and not generator!
  reporter = FlameGraphReporter.from_snapshot(allocations = desired_records, memory_records = memory_records, native_traces = reader.metadata.has_native_traces)
  print("[Info]: Reported created!!", flush = True)
  # Render.(TODO: update the memray code to instead take an `io.Bytes.IO` like object to write to rather than a disk file  to speed it up, as would be sending that html data back in some cases!)
  print("----------------------------------------", flush=True)
  with open(f"flamegraph_{os_thread_id}.html", "w", encoding = "utf8") as fxx:
    reporter.render(
      fxx,
      metadata = reader.metadata,    # it is just shown as it is in `stats` in generated html!,
      show_memory_leaks = False,     # False, until we gain more understanding..could be useful, for underlying C extensions to detect.. but specialist tools for this would be more suited anyWAY!
      merge_threads = False,
      inverted = False
    )
  print(f"Html generated at: flamegraph_{os_thread_id}.html", flush = True)


def deinit_memray_extended():
  # Should be called after  `del tracker` or `tracker.__exit__` i.e that tracker won't be used anymore from user side!
  global py_to_cpp_mapping, py_to_cpp_mapping_lock, py_to_cpp_mapping_payload, n_max_threads_tracked, memray_bin_fd, f_python_side

  f_python_side.close()
  del f_python_side
  del py_to_cpp_mapping
  del py_to_cpp_mapping_lock
