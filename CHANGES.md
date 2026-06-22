## About:
Current Memray architecture aims for `single` `Tracker` instance for a single/wide process, and rightly so, as it involves a delicate dance of tracking all allocations (including loaded native C/C++ extensions), without slowing down the actual logic of python programme, still handling/incorporating Python GIL without any deadlocking. 
But it doesn't play well when we would want to track allocations at thread-specific level, like for a web-server handling each request in a dedicated thread, (either by creating a new thread, or using a worker from an already created thread pool), so as to pin/map all allocations for that specific thread during some interval (extending that mapping to a particular `request` just executed, eventually passing all that information to the CLIENT/User).
Also it seems memray bundles a whole-lot of dependencies (may be for a plug and play experience) like `jinja`, `textual`??, and i cannot just shake the feeling that some of this code has been written using LLMs (too verbose and sprinkled through a large number of files!).Removing those extra dependencies would be secondary to the main goal, i don't write/read C++, so reading and understand some piece of code would take some time!

## Architecture: 
Based on my understanding, memray does able to track all allocations while being much faster than most of such profilers by using some clever tactics which work at much lower level than the Python code, and that all such code has to go through!
First step seems to be modifying the Process Linkage table (PLT) of the `Python` process, so as to `trap` all allocations calls.
Memray provides its own wrappers for most of common (user-space) allocation calls like `malloc`, `calloc` , this PLT table is modified on context entering `with memray.Tracker(.....)` like calls, and `switched` back to original when context exits aka `__exit__` call. Since it works at `linker` level, and irrespective of `code` which initiates it all such allocations are trapped, hence for a (python -> c_extension -> malloc) it remains possible to `map` such allocation to corresponding `python` code, (and to C/C++ code as well as it `native-traces` are ON... which i think is facilitated by libraries like `lib-unwind` by making native `stack-trace` available! ). Memray seems to do extra work to `merge` native and `python` stack traces, to have a nice unified view for user to look at.
Memray remains careful to not call `CPython` API during such `allocation` calls, as any CALL to CPython APIs are deemed to be unsafe, before *cleanly* acquiring the GIL which would required extra book-keeping particular for `dll/.so` code, which don't seem to know about `existence` of CPython.dll !
Also there is a high possiblity of DEAD-LOCKING, as any such call could lead to a new `allocation` request, resulting in a `recursive` `malloc/calloc` calls....
Memray solves a lot of these problems by keeping a shadow STACK, each `Python` function call enter/exit events are traced to populate the SHADOW stack, and any trapped allocation call could then be mapped to most recent entry in the stack, such argument could also be extended to `native` traces to get more fine-grained level allocation tracking! Of-course memray does handle many edge cases and implementation requires some careful thinking to put it all together for out of the box integration of memray for the python Code.

# Installation:
We modify the `setup.py` files to reduce the build pressure and speeding up compilation of memray for our development purposes, by removing the extra requirements. and also using the `parallel` builds if facilitated by `make` .
`ccache` is recommended to speed up compilations, another cool tool that just works !
Also passing `-no-index --no-build-isolation`, so such a command should work. (assuming you have setup a virtual environment as recommended in README.md)
```cmd
python3 -m pip install -e . --no-index --no-build-isolation
```
