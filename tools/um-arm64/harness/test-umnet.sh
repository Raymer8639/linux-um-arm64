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

printf '%s\n' 'umnet mock integration passed'
