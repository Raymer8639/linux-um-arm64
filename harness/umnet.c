// SPDX-License-Identifier: GPL-2.0-or-later

#define _GNU_SOURCE

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <unistd.h>

#define UMNET_MAX_FRAME 65535U
#define UMNET_WIRE_HEADER sizeof(uint32_t)
#define UMNET_BUFFER_SIZE (UMNET_MAX_FRAME + UMNET_WIRE_HEADER)

struct umnet_bridge {
	int uml_fd;
	int passt_fd;
	unsigned char to_passt[UMNET_BUFFER_SIZE];
	size_t to_passt_length;
	size_t to_passt_offset;
	unsigned char from_passt[UMNET_BUFFER_SIZE];
	size_t from_passt_length;
	bool uml_eof;
	bool passt_eof;
	bool uml_write_shutdown;
	bool passt_write_shutdown;
};

static void umnet_usage(const char *program)
{
	fprintf(stderr, "Usage: %s --uml-fd=N --passt-fd=N\n", program);
}

static int umnet_parse_fd(const char *value, int *result)
{
	char *end;
	long parsed;

	if (value == NULL || value[0] == '\0')
		return -1;
	errno = 0;
	parsed = strtol(value, &end, 10);
	if (errno != 0 || end == value || *end != '\0' || parsed < 0 ||
	    parsed > INT_MAX)
		return -1;
	*result = (int)parsed;
	return 0;
}

static int umnet_socket_type(int fd, int expected)
{
	int type;
	socklen_t length = sizeof(type);

	if (getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &length) < 0 ||
	    type != expected)
		return -1;
	return 0;
}

static int umnet_nonblocking(int fd)
{
	int flags = fcntl(fd, F_GETFL);

	if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0)
		return -1;
	return 0;
}

static int umnet_peek_frame(const struct umnet_bridge *bridge,
			    size_t *frame_length)
{
	uint32_t network_length;
	uint32_t length;

	if (bridge->from_passt_length < UMNET_WIRE_HEADER)
		return 0;
	memcpy(&network_length, bridge->from_passt, sizeof(network_length));
	length = ntohl(network_length);
	if (length == 0 || length > UMNET_MAX_FRAME)
		return -1;
	if (bridge->from_passt_length < UMNET_WIRE_HEADER + length)
		return 0;
	if (frame_length != NULL)
		*frame_length = length;
	return 1;
}

static int umnet_read_uml(struct umnet_bridge *bridge)
{
	struct msghdr message = { 0 };
	struct iovec vector;
	unsigned char frame[UMNET_MAX_FRAME];
	ssize_t length;
	uint32_t network_length;

	vector.iov_base = frame;
	vector.iov_len = sizeof(frame);
	message.msg_iov = &vector;
	message.msg_iovlen = 1;
	length = recvmsg(bridge->uml_fd, &message, MSG_TRUNC);
	if (length == 0) {
		bridge->uml_eof = true;
		return 0;
	}
	if (length < 0) {
		if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
			return 0;
		return -1;
	}
	if ((message.msg_flags & MSG_TRUNC) != 0 ||
	    (size_t)length > UMNET_MAX_FRAME)
		return -1;
	network_length = htonl((uint32_t)length);
	memcpy(bridge->to_passt, &network_length, sizeof(network_length));
	memcpy(bridge->to_passt + UMNET_WIRE_HEADER, frame, (size_t)length);
	bridge->to_passt_length = UMNET_WIRE_HEADER + (size_t)length;
	bridge->to_passt_offset = 0;
	return 0;
}

static int umnet_read_passt(struct umnet_bridge *bridge)
{
	ssize_t length;
	size_t available = sizeof(bridge->from_passt) -
		bridge->from_passt_length;

	if (available == 0)
		return -1;
	length = read(bridge->passt_fd,
		      bridge->from_passt + bridge->from_passt_length, available);
	if (length == 0) {
		int frame_state = umnet_peek_frame(bridge, NULL);

		if (bridge->from_passt_length == 0) {
			bridge->passt_eof = true;
			return 0;
		}
		if (frame_state != 1)
			return -1;
		bridge->passt_eof = true;
		return 0;
	}
	if (length < 0) {
		if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
			return 0;
		return -1;
	}
	bridge->from_passt_length += (size_t)length;
	return umnet_peek_frame(bridge, NULL) < 0 ? -1 : 0;
}

static int umnet_write_passt(struct umnet_bridge *bridge)
{
	ssize_t length;

	while (bridge->to_passt_offset < bridge->to_passt_length) {
		length = write(bridge->passt_fd,
			       bridge->to_passt + bridge->to_passt_offset,
			       bridge->to_passt_length - bridge->to_passt_offset);
		if (length > 0) {
			bridge->to_passt_offset += (size_t)length;
			continue;
		}
		if (length < 0 && (errno == EINTR))
			continue;
		if (length < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
			return 0;
		return -1;
	}
	bridge->to_passt_length = 0;
	bridge->to_passt_offset = 0;
	return 0;
}

static int umnet_write_uml(struct umnet_bridge *bridge)
{
	size_t frame_length;
	ssize_t length;
	int frame_state;

	frame_state = umnet_peek_frame(bridge, &frame_length);
	if (frame_state < 0)
		return -1;
	if (frame_state == 0)
		return 0;
	length = send(bridge->uml_fd,
		      bridge->from_passt + UMNET_WIRE_HEADER, frame_length,
		      MSG_NOSIGNAL);
	if (length < 0 && (errno == EINTR || errno == EAGAIN ||
			   errno == EWOULDBLOCK))
		return 0;
	if (length < 0 || (size_t)length != frame_length)
		return -1;
	bridge->from_passt_length -= UMNET_WIRE_HEADER + frame_length;
	if (bridge->from_passt_length != 0)
		memmove(bridge->from_passt,
			bridge->from_passt + UMNET_WIRE_HEADER + frame_length,
			bridge->from_passt_length);
	return 0;
}

static int umnet_run(struct umnet_bridge *bridge)
{
	for (;;) {
		struct pollfd descriptors[2];
		short uml_events = 0;
		short passt_events = 0;
		int result;
		size_t frame_length;
		int frame_state;

		if (bridge->uml_eof && bridge->to_passt_length == 0 &&
		    !bridge->passt_write_shutdown) {
			if (shutdown(bridge->passt_fd, SHUT_WR) < 0 && errno != ENOTCONN)
				return -1;
			bridge->passt_write_shutdown = true;
		}
		if (bridge->passt_eof && bridge->from_passt_length == 0 &&
		    !bridge->uml_write_shutdown) {
			/* UML uses a datagram socket: it has no half-close operation. */
			bridge->uml_write_shutdown = true;
		}
		if (bridge->passt_eof && bridge->to_passt_length == 0 &&
		    bridge->from_passt_length == 0)
			return 0;
		if (bridge->passt_eof && bridge->from_passt_length != 0 &&
		    umnet_peek_frame(bridge, NULL) != 1)
			return -1;

		if (!bridge->uml_eof && bridge->to_passt_length == 0)
			uml_events |= POLLIN;
		frame_state = umnet_peek_frame(bridge, &frame_length);
		if (frame_state < 0)
			return -1;
		if (frame_state > 0)
			uml_events |= POLLOUT;
		if (!bridge->passt_eof && bridge->from_passt_length <
				 sizeof(bridge->from_passt))
			passt_events |= POLLIN;
		if (bridge->to_passt_length != 0)
			passt_events |= POLLOUT;

		descriptors[0] = (struct pollfd){
			.fd = bridge->uml_fd,
			.events = uml_events,
		};
		descriptors[1] = (struct pollfd){
			.fd = bridge->passt_fd,
			.events = passt_events,
		};
		result = poll(descriptors, 2, -1);
		if (result < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		if ((descriptors[1].revents & (POLLIN | POLLERR | POLLHUP)) &&
		    umnet_read_passt(bridge) < 0)
			return -1;
		if (bridge->passt_eof && bridge->to_passt_length == 0 &&
		    bridge->from_passt_length == 0)
			return 0;
		if (descriptors[0].revents & (POLLERR | POLLNVAL))
			return -1;
		if (descriptors[1].revents & (POLLERR | POLLNVAL))
			return -1;
		if ((descriptors[0].revents & POLLOUT) &&
		    umnet_write_uml(bridge) < 0)
			return -1;
		if ((descriptors[1].revents & POLLOUT) &&
		    umnet_write_passt(bridge) < 0)
			return -1;
		if ((descriptors[0].revents & POLLIN) &&
		    umnet_read_uml(bridge) < 0)
			return -1;
		if ((descriptors[0].revents & POLLHUP) &&
		    !(descriptors[0].revents & POLLIN))
			bridge->uml_eof = true;
	}
}

int main(int argc, char **argv)
{
	struct umnet_bridge bridge = { .uml_fd = -1, .passt_fd = -1 };
	bool uml_set = false;
	bool passt_set = false;
	int index;

	for (index = 1; index < argc; index++) {
		if (strncmp(argv[index], "--uml-fd=", 9) == 0) {
			if (uml_set || umnet_parse_fd(argv[index] + 9,
						      &bridge.uml_fd) < 0)
				goto usage;
			uml_set = true;
		} else if (strncmp(argv[index], "--passt-fd=", 11) == 0) {
			if (passt_set || umnet_parse_fd(argv[index] + 11,
						       &bridge.passt_fd) < 0)
				goto usage;
			passt_set = true;
		} else {
			goto usage;
		}
	}
	if (!uml_set || !passt_set || bridge.uml_fd == bridge.passt_fd ||
	    umnet_socket_type(bridge.uml_fd, SOCK_DGRAM) < 0 ||
	    umnet_socket_type(bridge.passt_fd, SOCK_STREAM) < 0 ||
	    umnet_nonblocking(bridge.uml_fd) < 0 ||
	    umnet_nonblocking(bridge.passt_fd) < 0) {
		fprintf(stderr, "umnet: invalid fd configuration\n");
		return EXIT_FAILURE;
	}
	signal(SIGPIPE, SIG_IGN);
	return umnet_run(&bridge) == 0 ? EXIT_SUCCESS : EXIT_FAILURE;

usage:
	umnet_usage(argv[0]);
	return EXIT_FAILURE;
}
