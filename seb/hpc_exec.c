/* Drop namespace capabilities before executing untrusted task commands. */
#include <errno.h>
#include <linux/capability.h>
#include <stdio.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc < 2) return 64;
    for (int cap = 0; cap <= CAP_LAST_CAP; ++cap) {
        if (prctl(PR_CAPBSET_DROP, cap, 0, 0, 0) < 0 && errno != EINVAL) {
            perror("capability bounding set"); return 126;
        }
    }
    struct __user_cap_header_struct h = {_LINUX_CAPABILITY_VERSION_3, 0};
    struct __user_cap_data_struct d[2] = {{0}, {0}};
    if (syscall(SYS_capset, &h, d) < 0 || prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0) {
        perror("drop capabilities"); return 126;
    }
    execvp(argv[1], argv + 1);
    perror("exec"); return 127;
}
