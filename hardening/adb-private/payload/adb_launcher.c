/* 托管 ADB 入口：参数与闸门前置，所有执行统一经过带锁策略。 */
#define _POSIX_C_SOURCE 200809L
#define _XOPEN_SOURCE 700
#define _DARWIN_C_SOURCE 1
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#if !defined(ROOT_PATH) || !defined(SOCKET_PATH) || !defined(PYTHON)
#error "安装时必须提供本机路径"
#endif
#define SOCKET_SPEC "localfilesystem:" SOCKET_PATH

static void receipt(void) {
    const char *nonce = getenv("DSB_ADB_RECEIPT_NONCE");
    if (nonce && strlen(nonce) == 64 && strspn(nonce, "0123456789abcdef") == 64)
        fprintf(stderr, "ADB_POLICY_RECEIPT:%s\n", nonce);
}

static int denied(const char *reason) {
    fprintf(stderr, "ADB_POLICY_DENIED: %s\n", reason);
    receipt();
    return 78;
}

static ssize_t read_small(const char *path, char *out, size_t size) {
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NONBLOCK | O_NOFOLLOW);
    if (fd < 0) return -1;
    ssize_t n = read(fd, out, size - 1);
    int saved = errno;
    close(fd);
    errno = saved;
    if (n >= 0) out[n] = '\0';
    return n;
}

static int pause_check(void) {
    const char *paths[] = {ROOT_PATH "/maintenance", ROOT_PATH "/blocked"};
    const char *names[] = {"maintenance", "blocked"};
    for (size_t i = 0; i < 2; ++i) {
        struct stat info;
        if (lstat(paths[i], &info) < 0) {
            if (errno == ENOENT) continue;
            fprintf(stderr, "ADB_POLICY_PAUSED: %s gate: state unreadable (%s)\n", names[i], strerror(errno));
            receipt();
            return 78;
        }
        char reason[513];
        if (read_small(paths[i], reason, sizeof(reason)) <= 0)
            strcpy(reason, "pause reason unavailable; run doctor");
        reason[strcspn(reason, "\r\n")] = '\0';
        fprintf(stderr, "ADB_POLICY_PAUSED: %s gate: %s\n", names[i], reason);
        receipt();
        return 78;
    }
    return 0;
}

static bool same_file(const char *left, const char *right) {
    struct stat a, b;
    return stat(left, &a) == 0 && stat(right, &b) == 0 && a.st_dev == b.st_dev && a.st_ino == b.st_ino;
}

static bool private_endpoint(const char *input) {
    char directory[PATH_MAX], candidate[PATH_MAX], parent[PATH_MAX];
    snprintf(directory, sizeof(directory), "%s", SOCKET_PATH);
    char *slash = strrchr(directory, '/');
    if (!slash) return true;
    *slash = '\0';
    char resolved_directory[PATH_MAX];
    if (realpath(directory, resolved_directory)) strcpy(directory, resolved_directory);
    const char *path = realpath(input, candidate) ? candidate : input;
    if (path == input) {
        if (strlen(input) >= sizeof(parent)) return true;
        strcpy(parent, input);
        char *last = strrchr(parent, '/');
        const char *base = last ? last + 1 : input;
        char resolved[PATH_MAX];
        bool resolved_parent;
        if (last) {
            if (last == parent) resolved_parent = realpath("/", resolved) != NULL;
            else { *last = '\0'; resolved_parent = realpath(parent, resolved) != NULL; }
        } else resolved_parent = getcwd(resolved, sizeof(resolved)) != NULL;
        if (resolved_parent) {
            if (snprintf(candidate, sizeof(candidate), "%s/%s", resolved, base) >= (int)sizeof(candidate)) return true;
            path = candidate;
        }
    }
    size_t n = strlen(directory);
    if (strncmp(path, directory, n) == 0 && (path[n] == '\0' || path[n] == '/')) return true;
    if (same_file(input, SOCKET_PATH) || same_file(input, directory)) return true;
    if (strlen(input) >= sizeof(parent)) return true;
    strcpy(parent, input);
    slash = strrchr(parent, '/');
    if (slash) { *slash = '\0'; if (same_file(parent, directory)) return true; }
    return false;
}

static int validate(int argc, char **argv) {
    int i = 0;
    while (i < argc) {
        const char *arg = argv[i];
        if (strncmp(arg, "-H", 2) == 0 || strncmp(arg, "-P", 2) == 0 || strncmp(arg, "-a", 2) == 0)
            return denied("server host/port/all-interface overrides are disabled");
        if (strcmp(arg, "-L") == 0) {
            if (i + 1 >= argc || strcmp(argv[i + 1], SOCKET_SPEC) != 0)
                return denied("only the managed filesystem socket is supported");
            i += 2;
        } else if (strncmp(arg, "-L", 2) == 0 || strcmp(arg, "fork-server") == 0 || strcmp(arg, "--reply-fd") == 0) {
            return denied("unsupported internal or endpoint option");
        } else if (strcmp(arg, "-s") == 0 || strcmp(arg, "-t") == 0 || strcmp(arg, "--one-device") == 0) {
            if (i + 1 >= argc || argv[i + 1][0] == '\0' || argv[i + 1][0] == '-')
                return denied("missing global option value");
            i += 2;
        } else if (strcmp(arg, "server") == 0 || strcmp(arg, "nodaemon") == 0 ||
                   strcmp(arg, "-d") == 0 || strcmp(arg, "-e") == 0 || strcmp(arg, "--exit-on-write-error") == 0 ||
                   (strncmp(arg, "-s", 2) == 0 && arg[2] != '\0')) {
            ++i;
        } else if (arg[0] == '-') {
            return denied("unknown global option");
        } else break;
    }
    if (i < argc && strcmp(argv[i], "raw") == 0)
        return denied("raw host-service API is not a managed entry point");
    if (i < argc && (strcmp(argv[i], "forward") == 0 || strcmp(argv[i], "reverse") == 0)) {
        for (++i; i < argc; ++i)
            if (strncmp(argv[i], "localfilesystem:", 16) == 0 && private_endpoint(argv[i] + 16))
                return denied("private ADB directory cannot be bridged");
    }
    return 0;
}

int main(int argc, char **argv) {
    /* 解析检查不连接、不起服务，也不执行 backend。 */
    if (argc > 1 && strcmp(argv[1], "--policy-validate") == 0) return validate(argc - 2, argv + 2);
    int rc = pause_check();
    if (rc) return rc;
    rc = validate(argc - 1, argv + 1);
    if (rc) return rc;
    char **command = calloc((size_t)argc + 5, sizeof(char *));
    if (!command) return denied("cannot allocate command arguments");
    command[0] = PYTHON;
    command[1] = "-I";
    command[2] = ROOT_PATH "/adb_policy.py";
    command[3] = "--policy-start";
    for (int i = 1; i < argc; ++i) command[i + 3] = argv[i];
    execv(command[0], command);
    fprintf(stderr, "ADB_POLICY_PAUSED: startup/exec gate: %s\n", strerror(errno));
    receipt();
    free(command);
    return 78;
}
