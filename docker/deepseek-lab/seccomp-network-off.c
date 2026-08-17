#define _GNU_SOURCE

#include <errno.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

#define EXIT_LAUNCHER_FAILURE 125
#define DENY_SYSCALL(number)                                                  \
  BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (number), 0, 1),                      \
      BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | (EPERM & SECCOMP_RET_DATA))

static int fail(const char *message) {
  fprintf(stderr, "memoryos-network-off: %s: %s\n", message, strerror(errno));
  return EXIT_LAUNCHER_FAILURE;
}

static int install_filter(void) {
  struct sock_filter filter[] = {
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
      BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_X86_64, 1, 0),
      BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
      DENY_SYSCALL(__NR_socket),
      DENY_SYSCALL(__NR_socketpair),
      DENY_SYSCALL(__NR_connect),
      DENY_SYSCALL(__NR_bind),
      DENY_SYSCALL(__NR_listen),
      DENY_SYSCALL(__NR_accept),
      DENY_SYSCALL(__NR_accept4),
      DENY_SYSCALL(__NR_sendto),
      DENY_SYSCALL(__NR_sendmsg),
      DENY_SYSCALL(__NR_sendmmsg),
      DENY_SYSCALL(__NR_recvfrom),
      DENY_SYSCALL(__NR_recvmsg),
      DENY_SYSCALL(__NR_recvmmsg),
      DENY_SYSCALL(__NR_shutdown),
      DENY_SYSCALL(__NR_getsockname),
      DENY_SYSCALL(__NR_getpeername),
      DENY_SYSCALL(__NR_setsockopt),
      DENY_SYSCALL(__NR_getsockopt),
      BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
  };
  struct sock_fprog program = {
      .len = (unsigned short)(sizeof(filter) / sizeof(filter[0])),
      .filter = filter,
  };

  if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) {
    return fail("cannot set no_new_privs");
  }
  if (syscall(__NR_seccomp, SECCOMP_SET_MODE_FILTER, 0, &program) != 0) {
    return fail("cannot install seccomp filter");
  }
  return 0;
}

int main(int argc, char **argv) {
  if (argc < 2) {
    fprintf(stderr, "memoryos-network-off: usage: network-off <command> [args...]\n");
    return EXIT_LAUNCHER_FAILURE;
  }
  int code = install_filter();
  if (code != 0) return code;
  execvp(argv[1], &argv[1]);
  return fail("exec failed");
}
