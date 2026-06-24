# A simple test to see if our api even works!
import sys
import time

# This should point to our fork of memray!
print("[Info]: Memray being imported must point to modified copy of memray!")
import memray
from custom_api import deinit_memray_extended, resume_writing_allocations, suspend_writing_allocations,init_memray_extended

import threading

# Create Tracker instance.(just creates a writer technically)
tracker = init_memray_extended()

# This actually activates the allocations' tracking.
tracker.__enter__()

def worker(id:int):
  resume_writing_allocations()
  z_temp = bytearray(11_000_000) #allocate 11 Mb.
  print(len(z_temp))
  suspend_writing_allocations(True)

resume_writing_allocations()
# The following block will do some minor allocations in main threads to `bootstrap` the worker threads, and then those thread would allocate `concurrently`
# So we should get a set of 3 flamegraphs, depicting memory allocations on the disk on finishing of script!
t1 = threading.Thread(target = worker, args = (1,))
t2 = threading.Thread(target = worker, args = (2,))

t1.start()
t2.start()
t1.join()
t2.join()

# using True when to generate a cycle of allocations in a particualar thread, like finished processing a request !!
suspend_writing_allocations(True)

tracker.__exit__(None, None, None)
del tracker

deinit_memray_extended()
