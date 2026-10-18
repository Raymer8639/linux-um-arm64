#!/bin/sh

set -eu

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
temporary_directory=$(mktemp -d "${TMPDIR:-/tmp}/umnet-integration.XXXXXX")
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM

cc=${CC:-cc}
cflags=${CFLAGS:-}

cat >"$temporary_directory/mock-passt.c" <<'EOF'
#include <arpa/inet.h>
#include <errno.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int read_full(int fd, void *buffer, size_t length)
{
    size_t offset = 0;

    while (offset < length) {
        ssize_t result = read(fd, (char *)buffer + offset, length - offset);

        if (result == 0)
            return -1;
        if (result < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        offset += (size_t)result;
    }
    return 0;
}

static int write_bytes(int fd, const void *buffer, size_t length)
{
    const unsigned char *bytes = buffer;
    size_t offset;

    for (offset = 0; offset < length; offset++) {
        ssize_t result = write(fd, bytes + offset, 1);

        if (result < 0) {
            if (errno == EINTR) {
                offset--;
                continue;
            }
            return -1;
        }
    }
    return 0;
}

static int has_argument_pair(int argc, char **argv, const char *name,
                             const char *value)
{
    int index;

    for (index = 1; index + 1 < argc; index++)
        if (strcmp(argv[index], name) == 0 &&
            strcmp(argv[index + 1], value) == 0)
            return 1;
    return 0;
}

int main(int argc, char **argv)
{
    uint32_t network_length;
    uint32_t length;
    char *end = NULL;
    int fd = -1;
    int index;
    char *frame;
    const char *response = "host->guest";

    for (index = 1; index + 1 < argc; index++)
        if (strcmp(argv[index], "-F") == 0)
            fd = (int)strtol(argv[index + 1], &end, 10);
    if (fd < 0 || end == NULL || *end != '\0' ||
        !has_argument_pair(argc, argv, "-4", "-a"))
        return 2;
    if (read_full(fd, &network_length, sizeof(network_length)) < 0)
        return 3;
    length = ntohl(network_length);
    if (length != strlen("guest->host"))
        return 4;
    frame = malloc(length + 1);
    if (frame == NULL || read_full(fd, frame, length) < 0)
        return 5;
    frame[length] = '\0';
    if (strcmp(frame, "guest->host") != 0) {
        free(frame);
        return 6;
    }
    free(frame);
    network_length = htonl((uint32_t)strlen(response));
    if (write_bytes(fd, &network_length, sizeof(network_length)) < 0 ||
        write_bytes(fd, response, strlen(response)) < 0)
        return 7;
    return 0;
}
EOF

cat >"$temporary_directory/mock-uml.c" <<'EOF'
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    char *end;
    int fd = -1;
    int index;
    char buffer[64];
    ssize_t length;

    for (index = 1; index < argc; index++) {
        if (strncmp(argv[index], "vec0:transport=fd,fd=", 21) != 0)
            continue;
        fd = (int)strtol(argv[index] + 21, &end, 10);
        if (end == argv[index] + 21 || strncmp(end, ",mac=", 5) != 0)
            return 2;
    }
    if (fd < 0)
        return 3;
    if (send(fd, "guest->host", strlen("guest->host"), 0) < 0)
        return 4;
    length = recv(fd, buffer, sizeof(buffer) - 1, 0);
    if (length < 0 && errno == EINTR)
        length = recv(fd, buffer, sizeof(buffer) - 1, 0);
    if (length != (ssize_t)strlen("host->guest"))
        return 5;
    buffer[length] = '\0';
    return strcmp(buffer, "host->guest") == 0 ? 0 : 6;
}
EOF

"$cc" $cflags -std=c11 -Wall -Wextra -Werror -O2 \
    "$script_directory/umnet.c" -o "$temporary_directory/umnet"
"$cc" $cflags -std=c11 -Wall -Wextra -Werror -O2 \
    "$temporary_directory/mock-passt.c" -o "$temporary_directory/mock-passt"
"$cc" $cflags -std=c11 -Wall -Wextra -Werror -O2 \
    "$temporary_directory/mock-uml.c" -o "$temporary_directory/mock-uml"

set +e
"$temporary_directory/umnet" --passt "$temporary_directory/mock-passt" \
    -- "$temporary_directory/mock-uml" >"$temporary_directory/stdout" \
    2>"$temporary_directory/stderr"
status=$?
set -e
if [ "$status" -ne 0 ]; then
    cat "$temporary_directory/stdout" "$temporary_directory/stderr" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# IPv6: a second, IPv6-only passt, with guest frames routed by EtherType.
#
# The two helpers are separate processes on one simulated wire, so this checks
# both directions of the routing decision: an IPv4 frame must reach the -4
# helper, an 0x86dd frame must reach the -6 helper, and each reply must come
# back to the guest.
# ---------------------------------------------------------------------------
cat >"$temporary_directory/mock-passt-ipv6.c" <<'EOF'
#include <arpa/inet.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int read_full(int fd, void *buffer, size_t length)
{
    size_t offset = 0;

    while (offset < length) {
        ssize_t result = read(fd, (char *)buffer + offset, length - offset);

        if (result == 0)
            return -1;
        if (result < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        offset += (size_t)result;
    }
    return 0;
}

static int write_full(int fd, const void *buffer, size_t length)
{
    size_t offset;

    for (offset = 0; offset < length; offset++) {
        ssize_t result = write(fd, (const char *)buffer + offset, 1);

        if (result < 0) {
            if (errno == EINTR) {
                offset--;
                continue;
            }
            return -1;
        }
    }
    return 0;
}

int main(int argc, char **argv)
{
    unsigned char frame[64];
    unsigned char reply[64];
    uint32_t network_length;
    const char *tag;
    const char *dump_path;
    int is_ipv6 = 0;
    int fd = -1;
    int index;
    char *end = NULL;
    uint16_t ethertype;

    for (index = 1; index < argc; index++) {
        if (strcmp(argv[index], "-F") == 0 && index + 1 < argc) {
            fd = (int)strtol(argv[++index], &end, 10);
            if (end == NULL || *end != '\0')
                return 2;
        } else if (strcmp(argv[index], "-6") == 0) {
            is_ipv6 = 1;
        } else if (strcmp(argv[index], "-4") == 0) {
            is_ipv6 = 0;
        }
    }
    if (fd < 0)
        return 2;

    /* Optional: dump this helper's argv so a test can check what the bridge
     * passed to each side.  The IPv4 and IPv6 helpers are separate processes
     * with separate resolver options, so a value that reaches one is not
     * evidence that it reached the other. */
    dump_path = getenv("MOCK_ARGV_FILE");
    if (dump_path) {
        FILE *dump = fopen(dump_path, "a");

        if (dump) {
            fprintf(dump, "%s", is_ipv6 ? "v6" : "v4");
            for (index = 0; index < argc; index++)
                fprintf(dump, " %s", argv[index]);
            fprintf(dump, "\n");
            fclose(dump);
        }
    }

    /* Proves umnet keeps going when the helper it may do without dies. */
    if (is_ipv6 && getenv("MOCK_FAIL_V6"))
        return 3;

    if (read_full(fd, &network_length, sizeof(network_length)) < 0)
        return 4;
    network_length = ntohl(network_length);
    if (network_length < 14 || network_length > sizeof(frame))
        return 5;
    if (read_full(fd, frame, network_length) < 0)
        return 6;

    ethertype = (uint16_t)((frame[12] << 8) | frame[13]);
    if (is_ipv6 ? ethertype != 0x86dd : ethertype == 0x86dd)
        return 7;

    tag = is_ipv6 ? "v6-ok" : "v4-ok";
    memset(reply, 0, 14);
    memcpy(reply + 14, tag, strlen(tag));
    network_length = htonl((uint32_t)(14 + strlen(tag)));
    if (write_full(fd, &network_length, sizeof(network_length)) < 0 ||
        write_full(fd, reply, 14 + strlen(tag)) < 0)
        return 8;

    /* A real passt keeps serving; stand-in does too, until umnet kills it. */
    for (;;)
        pause();
}
EOF

cat >"$temporary_directory/mock-uml-ipv6.c" <<'EOF'
#include <arpa/inet.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

static const unsigned char v4_frame[16] = {
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x08, 0x00, 'v', '4'
};
static const unsigned char v6_frame[16] = {
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x86, 0xdd, 'v', '6'
};

int main(int argc, char **argv)
{
    struct timeval timeout = { .tv_sec = 5, .tv_usec = 0 };
    unsigned char reply[64];
    int want_v6 = 1;
    int fd = -1;
    int index;
    char *end;

    for (index = 1; index < argc; index++) {
        if (strncmp(argv[index], "vec0:transport=fd,fd=", 21) != 0)
            continue;
        fd = (int)strtol(argv[index] + 21, &end, 10);
        if (end == argv[index] + 21 || strncmp(end, ",mac=", 5) != 0)
            return 2;
    }
    if (fd < 0)
        return 3;
    if (getenv("MOCK_SKIP_V6"))
        want_v6 = 0;
    if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)))
        return 4;
    if (send(fd, v4_frame, sizeof(v4_frame), 0) < 0)
        return 5;
    if (want_v6 && send(fd, v6_frame, sizeof(v6_frame), 0) < 0)
        return 6;
    if (recv(fd, reply, sizeof(reply), 0) < 0)
        return 7;
    if (memcmp(reply + 14, "v4-ok", 5) != 0)
        return 8;
    if (!want_v6)
        return 0;
    if (recv(fd, reply, sizeof(reply), 0) < 0)
        return 9;
    if (memcmp(reply + 14, "v6-ok", 5) != 0)
        return 10;
    return 0;
}
EOF

"$cc" $cflags -std=c11 -Wall -Wextra -Werror -O2 \
    "$temporary_directory/mock-passt-ipv6.c" -o "$temporary_directory/mock-passt-ipv6"
"$cc" $cflags -std=c11 -Wall -Wextra -Werror -O2 \
    "$temporary_directory/mock-uml-ipv6.c" -o "$temporary_directory/mock-uml-ipv6"

set +e
"$temporary_directory/umnet" --passt "$temporary_directory/mock-passt-ipv6" \
    --ipv6 -- "$temporary_directory/mock-uml-ipv6" \
    >"$temporary_directory/stdout6" 2>"$temporary_directory/stderr6"
status=$?
set -e
if [ "$status" -ne 0 ]; then
    cat "$temporary_directory/stdout6" "$temporary_directory/stderr6" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# --dns none / --ipv6-dns none must reach the helpers as "-D none".
#
# Left to passt, it would read the host's resolv.conf (there is none on
# Android) and announce a resolver the bridge just promised not to announce.
# The bridge settles the nameserver for the normal path before forking, so the
# "none" case has to be as explicit as a value, and per helper: the two
# resolvers are separate options.
# ---------------------------------------------------------------------------
argv_file="$temporary_directory/argv-dns-none"
: >"$argv_file"
MOCK_ARGV_FILE="$argv_file" \
    "$temporary_directory/umnet" --passt "$temporary_directory/mock-passt-ipv6" \
    --dns none --ipv6 --ipv6-dns none \
    -- "$temporary_directory/mock-uml-ipv6" \
    >"$temporary_directory/stdout6c" 2>"$temporary_directory/stderr6c"
status=$?
if [ "$status" -ne 0 ]; then
    cat "$temporary_directory/stdout6c" "$temporary_directory/stderr6c" >&2
    exit 1
fi
if ! grep -Eq '^v4 .* -D none( |$)' "$argv_file"; then
    cat "$argv_file" >&2
    echo 'umnet did not pass -D none to the IPv4 helper' >&2
    exit 1
fi
if ! grep -Eq '^v6 .* -D none( |$)' "$argv_file"; then
    cat "$argv_file" >&2
    echo 'umnet did not pass -D none to the IPv6 helper' >&2
    exit 1
fi
if ! grep -q 'announcing no nameserver (--dns none)' \
        "$temporary_directory/stderr6c" ||
   ! grep -q 'announcing no IPv6 nameserver (--ipv6-dns none)' \
        "$temporary_directory/stderr6c"; then
    cat "$temporary_directory/stderr6c" >&2
    echo 'umnet did not say it announces no nameserver' >&2
    exit 1
fi

# Without --ipv6-dns the IPv4 setting must not disable IPv6 DNS: the two
# resolvers are separate options, and the IPv6 helper still announces one.
argv_file="$temporary_directory/argv-dns-none-v4-only"
: >"$argv_file"
MOCK_ARGV_FILE="$argv_file" \
    "$temporary_directory/umnet" --passt "$temporary_directory/mock-passt-ipv6" \
    --dns none --ipv6 \
    -- "$temporary_directory/mock-uml-ipv6" \
    >"$temporary_directory/stdout6d" 2>"$temporary_directory/stderr6d"
status=$?
if [ "$status" -ne 0 ]; then
    cat "$temporary_directory/stdout6d" "$temporary_directory/stderr6d" >&2
    exit 1
fi
if ! grep -Eq '^v4 .* -D none( |$)' "$argv_file"; then
    cat "$argv_file" >&2
    echo 'umnet did not pass -D none to the IPv4 helper' >&2
    exit 1
fi
if grep -Eq '^v6 .* -D none( |$)' "$argv_file"; then
    cat "$argv_file" >&2
    echo '--dns none leaked into the IPv6 helper resolver' >&2
    exit 1
fi

# An IPv6 helper that cannot start must not take IPv4 with it, and must say so.
set +e
MOCK_FAIL_V6=1 MOCK_SKIP_V6=1 \
    "$temporary_directory/umnet" --passt "$temporary_directory/mock-passt-ipv6" \
    --ipv6 -- "$temporary_directory/mock-uml-ipv6" \
    >"$temporary_directory/stdout6b" 2>"$temporary_directory/stderr6b"
status=$?
set -e
if [ "$status" -ne 0 ]; then
    cat "$temporary_directory/stdout6b" "$temporary_directory/stderr6b" >&2
    exit 1
fi
if ! grep -q 'umnet: ipv6 helper exited' "$temporary_directory/stderr6b"; then
    cat "$temporary_directory/stderr6b" >&2
    echo 'umnet did not report the missing IPv6 helper' >&2
    exit 1
fi

printf '%s\n' 'umnet mock integration passed'
