#include <unistd.h>
/* Rehearsal hf: a native binary, as the HF
   provider requires, that runs the fake script. PYTHON and SCRIPT are given at build time (tools/rehearsal/flow.py
   install_fake_hf), so nothing here names a machine path. */
#ifndef PYTHON
#error "build with -DPYTHON=\"/path/to/python\""
#endif
#ifndef SCRIPT
#error "build with -DSCRIPT=\"/path/to/fake_hf.py\""
#endif
int main(int argc, char **argv) {
    char *a[argc + 2];
    a[0] = PYTHON;
    a[1] = SCRIPT;
    for (int i = 1; i < argc; i++) a[i + 1] = argv[i];
    a[argc + 1] = 0;
    execv(a[0], a);
    return 127;
}
