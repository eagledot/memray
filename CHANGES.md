## About:
Current Memray architecture aims for `single` `Tracker` instance for a single/wide process, and rightly so, as it involves a delicate dance of tracking all allocations (including loaded native C/C++ extensions), without slowing down the actual logic of python programme, still handling/incorporating Python GIL without any deadlocking. Having a single writer/tracker may make it a bit easier to deal with such constraints!
But it doesn't play well when we would want to track allocations at thread-specific level, like for a web-server handling each request in a dedicated thread, (either by creating a new thread, or using a worker from an already created thread pool), so as to pin/map all allocations for that specific thread during some interval (extending that mapping to a particular `request` just executed, eventually passing all that information to the CLIENT/User).
Also it seems memray bundles a whole-lot of dependencies (may be for a plug and play experience) like `jinja`, `textual`??, and i cannot just shake the feeling that some of this code has been written using LLMs (too verbose and sprinkled through a large number of files!).Removing those extra dependencies would be secondary to the main goal, i don't write/read C++, so reading and understand some piece of code would take some time!

## Architecture: 
Based on my understanding, memray does able to track all allocations while being much faster than most of such profilers by using some clever tactics which work at much lower level than the Python code, and that all such code has to go through!
First step seems to be modifying the Process Linkage table (PLT) of the `Python` process, so as to `trap` all allocations calls.
Memray provides its own wrappers for most of common (user-space) allocation calls like `malloc`, `calloc` , this PLT table is modified on context entering `with memray.Tracker(.....)` like calls, and `switched` back to original when context exits aka `__exit__` call. Since it works at `linker` level, and irrespective of `code` which initiates it all such allocations are trapped, hence for a (python -> c_extension -> malloc) it remains possible to `map` such allocation to corresponding `python` code, (and to C/C++ code as well as it `native-traces` are ON... which i think is facilitated by libraries like `lib-unwind` by making native `stack-trace` available! ). Memray seems to do extra work to `merge` native and `python` stack traces, to have a nice unified view for user to look at.
Memray remains careful to not call `CPython` API during such `allocation` calls, as any CALL to CPython APIs are deemed to be unsafe, before *cleanly* acquiring the GIL which would required extra book-keeping particular for `dll/.so` code, which don't seem to know about `existence` of CPython.dll !
Also there is a high possiblity of DEAD-LOCKING, as any such call could lead to a new `allocation` request, resulting in a `recursive` `malloc/calloc` calls....
Memray bypass some of these problems by keeping a Shadow Stack, each `Python` function call enter/exit event is traced to populate this Shadow stack and any trapped allocation call could then be mapped to most recent entry in the stack, such argument could also be extended to `native` traces to get more fine-grained level allocation tracking! Of-course memray does handle many edge cases and implementation requires some careful thinking to put it all together for out of the box integration of memray for the python Code.

# Installation:
We modify the `setup.py` files to reduce the build pressure and speeding up compilation of memray for our development purposes, by removing the extra requirements. and also using the `parallel` builds if facilitated by `make` .
`ccache` is recommended to speed up compilations, another cool tool that just works !
Also passing `-no-index --no-build-isolation`, so such a command should work. (assuming you have setup a virtual environment as recommended in README.md)
```cmd
python3 -m pip install -e . --no-index --no-build-isolation
```

# Approach:
Initially there didn't seem to be an easy way to pass any thread specific information to C++ runtime, so as to have map allocations to a specific thread for some specified interval. Memray seems to generate a new `thread_id`, from an increasing atomic sequence, and stores in a Thread-local-variable for fast future accesses. (Since memray itself  would be loaded dynamically by the Python(parent process) *after* some time on demand(generally by `import` attribute i.e not at the startup)). `Setup.py` does check the availability of `Glibc.so` to use faster TLS storage model, as also confirmed by `https://maskray.me/blog/2021-02-14-all-about-thread-local-storage`. (i know very very little about TLS anyway, but some minor reading has helped me to know how TLS is deeply related to when a dynamic/shared object is loaded which would be leveraging TLS somewhere inside their code, which in turn would decide which TLS model would be applicable!)

Our main point is about calling of a particular constructor when such TLS variable would be first accessed, in this case this would be `generate_next_tid`, which would then be stored in a `thread-local` variable `tid`. We modify this to instead generate the underlying `Pid`, so that it would be same as the `python thread` responsible for initiating the `allocation` call in the first place. (We always assume python code is the real initiator of a chain of allocation calls, and since python code would be executed inside a *running* python thread.. we could attribute all allocations to a python thread !).

Given that we can now `map` each allocation to a particular python thread, we could now come up with way to `suspend` and `resume` allocations for a given thread. This would give us much more control than default `with memray.Tracker` context protocol. Code could look like this:

```python
tracker = memray.Tracker("xxxxxx.bin")  # create a writer
tracker.__enter__()                     # here actual tracker is created
....
resume_tracking()
...
suspend_tracking()

...
resume_tracking()
..
suspend_tracking()

tracker.__exit__()
```
To make suspend/resume work, we modify `Tracker` class definitions/declarations, to also accept a pointer to an array/buffer allocated by Python side. We use this array/buffer as a `mapping` from `process id` to `Flag (1/0)`, so that we could update corresponding flag for the `process id` from the python side at will. C++ code could then read this to stop writing records to underlying `.bin` file if it `reads` `0` for corresponding `process id` and vice-versa. We don't use any Common `lock`, even though this mapping/buffer could be read concurrently, and very rare chance of race-condition (which may cause it for miss atmost 1 allocation). 
But since `tracking` would be initiated through `Python` code, which would mean only 1 (python) thread could be running, so only after we set the Flag for that thread, the `interested` allocation would occur and hence there is an *order*  (which mean we will not miss any interesting allocation).  This seems to work very well in my testing, as same logic could be extend to all interesting allocations, as some Python thread would have explicitly have resumed the tracking at somepoint in the python code.
We modify the C++ code somewhat as shown below:
```C++
    unsigned int expected_process_id = (unsigned int)(syscall(SYS_gettid));
    bool write_record_for_this_allocation = false;
    int mapping_array_max_size =  ((int *)(this -> d_mapping))[0];      // logical elements, each being 4 byte (processId,1/0,processId,1/0,...) every second value is acting as a flag to resume or suspend tracking for this thread/process id.
    unsigned int * mapping = ((unsigned int *)(this -> d_mapping)) + 1;// 1 for offset, should increment by 4 in absolute sense!
    for (int i = 0; i < mapping_array_max_size; i+=2){
        if (mapping[i] == expected_process_id ){
              write_record_for_this_allocation  = (mapping[i + 1] == 1);
              break;
           }
    }
```
On the python side, we expose APIs `resume/suspend` tracking, which updates the `process-id` slot for 1/0, as required, so as the following code could work:
```python
...
tracker = memray.Tracker("xxxx.bin")
tracker.__enter__()
...
resume_tracking()
x = bytearray(1_000_000)
suspend_tracking()
...
tracker.__exit__()
```

# Example:
We make necessary changes to `flamegraph.py` to add a make-do API to generate `html` given allocations from, as Memray currently doesn't expose an API to generate flamegraph programmatically and instead suggest to follow `subprocess [memray flamegraph  ..]` route. Our Api just works good enough for now to let us generate such flamegraphs from python directly!
After adding some wrapping code like allocating `buffer` for `mapping` , we should now have enough functionality to showcase a full example as show below. It is same as `test_1.py` file in root of this `fork`.
```python
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
```

# Todo/Pending Issues:
* For now maximum RSS usage would not be "correctly" reflected in the generated Flamegraph, as original CODE still assumes `process` level STATS !
* To add some filtering either `index` based or `timestamp` based to collect stats for each independent `cycle` rather than accumulating, as there is still a single `.bin` file is assumed by original Memray code.
* Speed up flamegraph generation by modifying `reader` code to just use the data from the Memory rather than round-trips to DISK!!
* apply hook on `thread-exit` to reset the `Pid` stored in mapping, so as that space could be reused/freed for future use, since that particular Pid may not be returned/attributed for some time by OS (atleast in case of linux)!
