// SPDX-License-Identifier: GPL-2.0
/*
 * umnet -- give a UML guest a network without root, a tap device, or any
 * capability at all.
 *
 * The problem: every conventional UML network transport needs privileges the
 * 'shell' user on Android does not have. tap and raw need CAP_NET_ADMIN and
 * /dev/net/tun; gre and l2tpv3 need raw sockets. What is left is UML's 'fd'
 * transport, which simply reads and writes Ethernet frames on a descriptor
 * somebody else set up -- and a userspace TCP/IP stack on the other end of it,
 * which needs no privileges because it makes ordinary outbound socket calls on
 * the guest's behalf.
 *
 * passt is that stack. It is plain C with no dependencies beyond libc, so
 * unlike libslirp (which needs glib) it cross-compiles statically for Android
 * in one step.
 *
 * The one mismatch is framing, and it is why this program exists:
 *
 *   UML's fd transport does one frame per datagram, using recvmmsg/sendmmsg on
 *   the descriptor. Frame boundaries come from the socket.
 *
 *   passt speaks qemu's socket protocol on a stream: each frame is preceded by
 *   its length as a 4-byte big-endian integer. Frame boundaries come from the
 *   prefix.
 *
 * So umnet sits between them: a SOCK_DGRAM socketpair towards UML, a
 * SOCK_STREAM socketpair towards passt, and a pump that adds the length prefix
 * in one direction and strips it in the other. It then execs the UML kernel
 * with the guest end of the datagram pair inherited, appending the vector
 * argument that points at it.
 *
 * IPv6 is served by a second passt.
 *
 * Two helpers rather than one dual-stack passt: a helper that loses its IPv6
 * half must not take IPv4 down with it, and --passt-pid-file names the IPv4
 * helper whose loss ends the run. A single dual-stack passt is no longer
 * blocked by netlink -- passt-android's prepatched mode now configures both
 * families locally -- but keeping the families in separate processes keeps
 * the failure domains and the IPv4 path unchanged.
 *
 * The helper needs a passt built with that prepatched-IPv6 support. An older
 * passt opens NETLINK_ROUTE for IPv6, which Android denies to an app uid
 * (bind((0, 0)) returns EACCES), so the helper exits before its event loop;
 * umnet says so and the run continues with IPv4 only. The guest simply has no
 * IPv6, which is the honest outcome and is what the gate checks for.
 *
 * Usage:
 *   umnet [--passt PATH] [--passt-pid-file PATH] [--mac MAC] [--iface IF]
 *         [--dns ADDR|none]
 *         [--addr A] [--mask N] [--gw G]
 *         [--ipv6 [--ipv6-addr A[/P]] [--ipv6-gw G] [--ipv6-dns ADDR|none]]
 *         [--verbose] -- ./linux <uml args...>
 *
 * --dns defaults to the first IPv4 nameserver the host's resolv.conf names
 * ($PREFIX first for Termux, then /etc), and only falls back to 1.1.1.1 if
 * neither is readable.  --dns none announces no resolver at all.  --ipv6-dns
 * does the same for AAAA-capable resolvers; its fallback is the public
 * 2001:4860:4860::8888, because a v4 resolver cannot be reached by a guest
 * that has no IPv4 address.
 *
 * The UML side ends up with, appended to its command line:
 *   vec0:transport=fd,fd=<n>,mac=<mac>
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <stdbool.h>
#include <fcntl.h>
#include <poll.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <arpa/inet.h>

#define MAX_FRAME	65536
#define PFX		4		/* qemu socket protocol length prefix */
#define ETH_HLEN	14		/* Ethernet II header */

/*
 * One passt and the stream socket this process pumps frames over.  There is
 * always an IPv4 helper; the IPv6 one exists only with --ipv6.
 */
/*
 * How long a single read or write on a helper socket may block.
 *
 * The pump runs in the same process as the waitpid() that reaps the UML
 * kernel, so an unbounded write to a helper that stopped draining (SIGSTOP,
 * a resource-stalled passt) would stop umnet from ever noticing the guest
 * exited -- the very hang the poll()/waitpid loop exists to avoid.  A timeout
 * turns that into EAGAIN, and EAGAIN on this path already means "drop the
 * frame", which is the correct loss model for a network bridge.
 */
#define HELPER_IO_TIMEOUT_SEC 5

/*
 * Apply the I/O budget above to a helper's stream socket.
 */
static int helper_limit_io(int fd)
{
	struct timeval tv = { .tv_sec = HELPER_IO_TIMEOUT_SEC, .tv_usec = 0 };

	if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv)) ||
	    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv)))
		return -1;
	return 0;
}

struct helper {
	const char *name;	/* "IPv4" / "IPv6", for messages */
	pid_t pid;
	int fd;			/* our end of the stream to passt */
	bool alive;
	bool required;		/* its death ends the run */
};

static struct helper helpers[2];
static int nhelpers;

static pid_t uml_pid_global;		/* for the signal handler only */
static pid_t parent_pid;		/* umnet's own pid, for the PDEATHSIG check */
static int verbose;			/* --verbose: log every frame both ways */

/*
 * Take the whole run down with us.
 *
 * The kernel is our child and would otherwise outlive us: whoever started
 * umnet kills umnet, not the kernel, so a supervisor reaping its child left a
 * booted guest running with a socket nobody was pumping any more.  Kill the
 * kernel's process group as well as every helper; the group is what UML's own
 * children end up in.
 */
static void reap(int sig)
{
	int i;

	if (uml_pid_global > 0)
		kill(-uml_pid_global, SIGKILL);
	for (i = 0; i < nhelpers; i++)
		if (helpers[i].pid > 0 && helpers[i].alive)
			kill(helpers[i].pid, SIGTERM);
	/* An aborted run is not a successful one: report it the way a shell does. */
	_exit(128 + sig);
}

/* Read exactly n bytes, or return 0 on EOF / -1 on error. */
static int read_full(int fd, void *buf, size_t n)
{
	size_t done = 0;

	while (done < n) {
		ssize_t r = read(fd, (char *)buf + done, n - done);

		if (r == 0)
			return 0;
		if (r < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		done += r;
	}
	return 1;
}

static int write_full(int fd, const void *buf, size_t n)
{
	size_t done = 0;

	while (done < n) {
		ssize_t w = write(fd, (const char *)buf + done, n - done);

		if (w < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		done += w;
	}
	return 0;
}

/*
 * A helper that is allowed to disappear must not be waited for forever, and
 * its frames must not be written to a descriptor nobody reads.  Reap it now,
 * say what happened, and take it out of the poll set.
 */
/*
 * Reap a helper without blocking forever.
 *
 * Returns 1 and fills *status when the child was collected, 0 when it could
 * not be collected even after SIGKILL.  Called from the teardown path, which
 * must not be able to stall: the supervisor's exit status depends on it.
 */
static int reap_helper(pid_t pid, int *status)
{
	int attempt;

	for (attempt = 0; attempt < 50; attempt++) {
		pid_t done = waitpid(pid, status, WNOHANG);

		if (done == pid)
			return 1;
		if (done < 0)
			return 0;
		if (attempt == 20)
			kill(pid, SIGKILL);
		usleep(100000);
	}
	kill(pid, SIGKILL);
	return waitpid(pid, status, WNOHANG) == pid;
}

static void helper_gone(struct helper *h, const char *why)
{
	if (!h->alive)
		return;
	h->alive = false;

	if (verbose)
		fprintf(stderr, "umnet: %s helper gone: %s\n", h->name, why);

	if (h->pid > 0) {
		int reaped;

		kill(h->pid, SIGTERM);
		reaped = waitpid(h->pid, NULL, WNOHANG);
		/* Still alive is fine: teardown reaps it once it is gone. */
		if (reaped == h->pid || reaped < 0)
			h->pid = 0;
	}
	if (h->fd >= 0) {
		close(h->fd);
		h->fd = -1;
	}

	if (!h->required)
		fprintf(stderr,
			"umnet: ipv6 helper exited; continuing without IPv6\n");
}

/*
 * Move whatever is ready in one poll round.
 *
 * Returns 0 to keep pumping, -1 when the link is finished: the IPv4 helper
 * closed, or a frame could not be delivered to the guest.  An IPv6 helper
 * going away is not the end of the run -- IPv4 keeps working, and a caller
 * that asked for IPv6 learns about it from the message helper_gone() prints.
 */
static int pump_once(int uml_fd, int timeout_ms)
{
	static unsigned char frame[PFX + MAX_FRAME];
	struct helper *map[2];
	struct pollfd pfd[3];
	int np = 0, i, ready;
	bool link_dead = false;

	pfd[np].fd = uml_fd;
	pfd[np].events = POLLIN;
	pfd[np].revents = 0;
	np++;

	for (i = 0; i < nhelpers; i++) {
		if (!helpers[i].alive)
			continue;
		pfd[np].fd = helpers[i].fd;
		pfd[np].events = POLLIN;
		pfd[np].revents = 0;
		map[np - 1] = &helpers[i];
		np++;
	}

	ready = poll(pfd, np, timeout_ms);
	if (ready < 0)
		return errno == EINTR ? 0 : -1;
	if (ready == 0)
		return 0;

	/* guest -> host: datagram in, length-prefixed out to the right helper */
	if (pfd[0].revents & POLLIN) {
		ssize_t n = recv(uml_fd, frame + PFX, MAX_FRAME, 0);

		if (n <= 0 && !(n < 0 && errno == EINTR))
			return -1;
		if (n > 0) {
			struct helper *h = &helpers[0];
			uint32_t len = htonl((uint32_t)n);

			/*
			 * EtherType is at offset 12 of an Ethernet II frame.
			 * A runt frame cannot be classified, so it goes to
			 * IPv4, which drops it exactly as the wire would.
			 */
			if (n >= ETH_HLEN && frame[PFX + 12] == 0x86 &&
			    frame[PFX + 13] == 0xdd) {
				if (nhelpers > 1 && helpers[1].alive)
					h = &helpers[1];
				else
					h = NULL;	/* no IPv6 stack: drop */
			}

			if (verbose)
				fprintf(stderr, "umnet: guest->host %zd -> %s\n",
					n, h ? h->name : "none");
			if (h) {
				memcpy(frame, &len, PFX);
				if (write_full(h->fd, frame, PFX + n)) {
					if (h->required)
						return -1;
					helper_gone(h, "write failed");
				}
			}
		}
	}

	/* host -> guest: length-prefixed in, datagram out */
	for (i = 1; i < np; i++) {
		struct helper *h = map[i - 1];
		uint32_t len;
		int r;

		if (!h->alive)
			continue;

		if (pfd[i].revents & POLLIN) {
			r = read_full(h->fd, &len, PFX);
			if (r <= 0) {
				if (h->required) {
					/*
					 * Do not return yet: frames already
					 * delivered by the other helper are
					 * still worth handing to the guest.
					 */
					link_dead = true;
					continue;
				}
				helper_gone(h, "closed");
				continue;
			}
			len = ntohl(len);
			if (len == 0 || len > MAX_FRAME) {
				fprintf(stderr, "umnet: bad frame length %u\n",
					len);
				/* The optional helper's stream is untrustworthy now, but
				 * IPv4 is still serving: drop the helper, keep the link. */
				if (!h->required) {
					helper_gone(h, "bad frame length");
					continue;
				}
				return -1;
			}
			if (read_full(h->fd, frame + PFX, len) <= 0) {
				if (h->required) {
					link_dead = true;
					continue;
				}
				helper_gone(h, "closed mid-frame");
				continue;
			}
			if (verbose)
				fprintf(stderr, "umnet: host->guest %u\n", len);
			/*
			 * A datagram socket has no partial writes, and a frame
			 * dropped here is exactly what a dropped packet is on
			 * real hardware -- so failure is not fatal, the guest's
			 * TCP will retransmit.
			 */
			if (send(uml_fd, frame + PFX, len, MSG_DONTWAIT) < 0 &&
			    errno != EAGAIN && errno != EWOULDBLOCK &&
			    errno != EINTR)
				return -1;
		}

		if (h->alive && (pfd[i].revents & (POLLERR | POLLHUP))) {
			if (h->required) {
				link_dead = true;
				continue;
			}
			helper_gone(h, "hung up");
		}
	}

	return link_dead ? -1 : 0;
}

/*
 * Pick the nameserver to announce when the caller did not name one.
 *
 * The old default was the literal "1.1.1.1", and that is a guess about the
 * network rather than about the build.  When the network in question blocks
 * that one resolver -- which happened here -- the guest's lookups fail, the
 * gate reports FAIL, and nothing in the output says the address was invented
 * on this side.  So ask the host first.
 *
 * Android has no /etc/resolv.conf (DNS goes through netd), but Termux has one
 * under $PREFIX, and every ordinary Linux host has the other path.  Try both
 * and fall back to the old literal only if neither is readable.
 *
 * want_v6 selects the family: an IPv4 address cannot serve a guest that only
 * has IPv6, and vice versa.  Returns a malloc'd string, or NULL.
 */
static char *dns_from_resolv_conf(bool want_v6)
{
	char path[PATH_MAX];
	const char *candidates[3];
	int n = 0, i;

	const char *prefix = getenv("PREFIX");

	if (prefix && *prefix) {
		snprintf(path, sizeof(path), "%s/etc/resolv.conf", prefix);
		candidates[n++] = path;
	}
	candidates[n++] = "/etc/resolv.conf";
	candidates[n] = NULL;

	for (i = 0; i < n; i++) {
		char line[512];
		FILE *f = fopen(candidates[i], "r");

		if (!f)
			continue;
		while (fgets(line, sizeof(line), f)) {
			char *p = line, *end;

			while (*p == ' ' || *p == '\t')
				p++;
			if (strncmp(p, "nameserver", 10))
				continue;
			p += 10;
			while (*p == ' ' || *p == '\t')
				p++;
			end = p;
			while (*end && *end != '\n' && *end != '\r' &&
			       *end != ' ' && *end != '\t')
				end++;
			*end = '\0';
			if (!*p)
				continue;
			if ((strchr(p, ':') != NULL) != want_v6)
				continue;
			{
				char *ret = strdup(p);

				fclose(f);
				return ret;
			}
		}
		fclose(f);
	}
	return NULL;
}

/*
 * exec passt with the common options and the caller's stack-specific ones.
 * The -F descriptor is this process's end of the stream socket; the caller
 * closes every other descriptor before calling this.
 */
static void exec_passt(const char *path, int fd, const char *const *extra)
{
	char fdbuf[16];
	char *argv[48];
	int n = 0, i;

	/*
	 * The parent (this process) owns the isolation: it created the socketpair,
	 * it started the kernel, and on Android the kernel denies the namespaces
	 * passt would create.  Say so explicitly rather than letting passt infer
	 * it from the descriptor type, because that inference also applied to
	 * ordinary non-Android --fd callers and silently dropped their sandbox.
	 */
	setenv("PASST_ANDROID_NO_ISOLATION", "1", 1);

	snprintf(fdbuf, sizeof(fdbuf), "%d", fd);

	argv[n++] = "passt";
	/*
	 * -f  stay in the foreground, so this process is the child we can
	 *     signal and wait on rather than a daemon that outlives the guest
	 *     and holds the socket.
	 * -t/-u none  no inbound port forwarding: the guest gets outbound
	 *     connectivity, and nothing on the phone's network can reach into
	 *     it.
	 */
	argv[n++] = "-f";
	argv[n++] = "-F"; argv[n++] = fdbuf;
	argv[n++] = "-t"; argv[n++] = "none";
	argv[n++] = "-u"; argv[n++] = "none";
	for (i = 0; extra[i]; i++) {
		if (n >= (int)(sizeof(argv) / sizeof(argv[0])) - 1)
			break;
		argv[n++] = (char *)extra[i];
	}
	argv[n] = NULL;

	execv(path, argv);
	perror("umnet: exec passt");
	_exit(127);
}

/* Fork one helper and record it; the child closes the descriptors it must not
 * hold, so the parent's other helpers never keep a dead peer's stream open. */
static pid_t spawn_helper(const char *path, const char *name, int child_fd,
			  int parent_fd, const int *close_fds, int n_close,
			  const char *const *extra, bool required)
{
	pid_t pid = fork();

	if (pid < 0) {
		perror("umnet: fork");
		return -1;
	}
	if (pid == 0) {
		int i;

		for (i = 0; i < n_close; i++)
			close(close_fds[i]);
		exec_passt(path, child_fd, extra);
		_exit(127);
	}

	close(child_fd);
	if (helper_limit_io(parent_fd))
		perror("umnet: helper I/O timeout");
	helpers[nhelpers].name = name;
	helpers[nhelpers].pid = pid;
	helpers[nhelpers].fd = parent_fd;
	helpers[nhelpers].alive = true;
	helpers[nhelpers].required = required;
	nhelpers++;
	return pid;
}

int main(int argc, char **argv)
{
	const char *passt_path = "./passt";
	/*
	 * Where to write the helper's pid.  On this platform the helper cannot be
	 * found by looking: it is a live process of the same uid, it answers
	 * kill(pid, 0), and /proc does not list it.  (Confirmed from umnet's own
	 * waitpid while the helper served DHCP and TCP.)  So anything that has to
	 * signal the helper -- a fault-injection scenario dropping the link, a
	 * supervisor reaping it -- has to be told the pid by the process that
	 * forked it.  With --ipv6 this names the IPv4 helper, the one whose loss
	 * ends the run.
	 */
	const char *passt_pid_file = NULL;
	const char *mac = "02:00:00:00:00:01";
	const char *iface = NULL;	/* passt -i: which host interface to mirror */
	const char *dns = NULL;		/* passt -D; NULL means "work it out" */
	char *dns_alloc = NULL;		/* what dns_from_resolv_conf handed us */
	bool dns_disabled = false;	/* --dns none */
	const char *addr = "10.0.2.15";	/* passt -a: address given to the guest */
	const char *mask = "24";	/* passt -n */
	const char *gw = "10.0.2.2";	/* passt -g: gateway passt emulates */
	bool ipv6 = false;		/* --ipv6: second, IPv6-only helper */
	const char *ipv6_addr = "fd00::2/64";	/* passt -a for the v6 helper */
	const char *ipv6_gw = "fe80::1";	/* its gateway */
	const char *ipv6_dns = NULL;	/* its -D; NULL means "work it out" */
	char *ipv6_dns_alloc = NULL;
	bool ipv6_dns_disabled = false;
	const char *v4_extra[24];
	const char *v6_extra[16];
	int uml_sp[2], v4_sp[2], v6_sp[2];
	int n4 = 0, n6 = 0;
	int close_child[8];
	int umlarg, i;
	int status, rc = 0;
	int kernel_status;
	bool kernel_reaped;
	char vecarg[128];
	char **child;
	pid_t uml_pid;
	pid_t pid;

	for (i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--verbose"))
			verbose = 1;
		else if (!strcmp(argv[i], "--passt") && i + 1 < argc)
			passt_path = argv[++i];
		else if (!strcmp(argv[i], "--passt-pid-file") && i + 1 < argc)
			passt_pid_file = argv[++i];
		else if (!strcmp(argv[i], "--mac") && i + 1 < argc)
			mac = argv[++i];
		else if (!strcmp(argv[i], "--iface") && i + 1 < argc)
			iface = argv[++i];
		else if (!strcmp(argv[i], "--dns") && i + 1 < argc) {
			const char *v = argv[++i];

			if (!strcmp(v, "none"))
				dns_disabled = true;
			else
				dns = v;
		}
		else if (!strcmp(argv[i], "--addr") && i + 1 < argc)
			addr = argv[++i];
		else if (!strcmp(argv[i], "--mask") && i + 1 < argc)
			mask = argv[++i];
		else if (!strcmp(argv[i], "--gw") && i + 1 < argc)
			gw = argv[++i];
		else if (!strcmp(argv[i], "--ipv6"))
			ipv6 = true;
		else if (!strcmp(argv[i], "--ipv6-addr") && i + 1 < argc) {
			ipv6_addr = argv[++i];
			ipv6 = true;
		}
		else if (!strcmp(argv[i], "--ipv6-gw") && i + 1 < argc) {
			ipv6_gw = argv[++i];
			ipv6 = true;
		}
		else if (!strcmp(argv[i], "--ipv6-dns") && i + 1 < argc) {
			const char *v = argv[++i];

			if (!strcmp(v, "none"))
				ipv6_dns_disabled = true;
			else
				ipv6_dns = v;
			ipv6 = true;
		}
		else if (!strcmp(argv[i], "--"))
			break;
		else {
			fprintf(stderr, "umnet: unknown option %s\n", argv[i]);
			return 2;
		}
	}
	if (i >= argc || strcmp(argv[i], "--")) {
		fprintf(stderr,
			"usage: umnet [--passt PATH] [--mac MAC] [--iface IF] "
			"[--passt-pid-file P] [--dns ADDR|none] [--addr A] [--mask N] "
			"[--gw G] [--ipv6 [--ipv6-addr A[/P]] [--ipv6-gw G] "
			"[--ipv6-dns ADDR|none]] [--verbose] -- ./linux <args...>\n");
		return 2;
	}
	umlarg = i + 1;
	if (umlarg >= argc) {
		fprintf(stderr, "umnet: no UML command given\n");
		return 2;
	}

	/*
	 * Settle the nameserver before forking.  It must never be left to passt:
	 * passt reads /etc/resolv.conf, Android has none, and it then gives up
	 * with "No IPv4 nameserver available for DHCP" so the guest never gets a
	 * lease.
	 */
	if (dns_disabled) {
		fprintf(stderr, "umnet: announcing no nameserver (--dns none)\n");
	} else if (dns) {
		fprintf(stderr, "umnet: announcing nameserver %s (--dns)\n", dns);
	} else {
		dns_alloc = dns_from_resolv_conf(false);
		if (dns_alloc) {
			dns = dns_alloc;
			fprintf(stderr,
				"umnet: announcing nameserver %s (host resolv.conf)\n",
				dns);
		} else {
			dns = "1.1.1.1";
			fprintf(stderr,
				"umnet: announcing nameserver %s "
				"(fallback: no readable resolv.conf)\n", dns);
		}
	}

	if (ipv6) {
		if (ipv6_dns_disabled) {
			fprintf(stderr,
				"umnet: announcing no IPv6 nameserver (--ipv6-dns none)\n");
		} else if (ipv6_dns) {
			fprintf(stderr, "umnet: announcing IPv6 nameserver %s "
				"(--ipv6-dns)\n", ipv6_dns);
		} else {
			ipv6_dns_alloc = dns_from_resolv_conf(true);
			if (ipv6_dns_alloc) {
				ipv6_dns = ipv6_dns_alloc;
				fprintf(stderr, "umnet: announcing IPv6 nameserver "
					"%s (host resolv.conf)\n", ipv6_dns);
			} else {
				ipv6_dns = "2001:4860:4860::8888";
				fprintf(stderr, "umnet: announcing IPv6 nameserver "
					"%s (fallback: no IPv6 nameserver in "
					"resolv.conf)\n", ipv6_dns);
			}
		}
	}

	if (socketpair(AF_UNIX, SOCK_DGRAM, 0, uml_sp) ||
	    socketpair(AF_UNIX, SOCK_STREAM, 0, v4_sp) ||
	    (ipv6 && socketpair(AF_UNIX, SOCK_STREAM, 0, v6_sp))) {
		perror("umnet: socketpair");
		return 1;
	}

	/*
	 * Give the guest side a generous socket buffer. The default is small
	 * enough that a burst -- an apt download, say -- drops frames on the
	 * floor between passt and UML, which shows up as a stalled TCP
	 * connection rather than as an error anybody can see.
	 */
	{
		int sz = 1 << 20;
		int pairs[3][2] = { { uml_sp[0], uml_sp[1] },
				    { v4_sp[0], v4_sp[1] },
				    { -1, -1 } };
		int np = 2, k;

		if (ipv6) {
			pairs[2][0] = v6_sp[0];
			pairs[2][1] = v6_sp[1];
			np = 3;
		}
		for (k = 0; k < np; k++) {
			setsockopt(pairs[k][0], SOL_SOCKET, SO_RCVBUF,
				   &sz, sizeof(sz));
			setsockopt(pairs[k][0], SOL_SOCKET, SO_SNDBUF,
				   &sz, sizeof(sz));
			setsockopt(pairs[k][1], SOL_SOCKET, SO_RCVBUF,
				   &sz, sizeof(sz));
			setsockopt(pairs[k][1], SOL_SOCKET, SO_SNDBUF,
				   &sz, sizeof(sz));
		}
	}

	/*
	 * A helper that has gone away must not take the bridge with it through
	 * SIGPIPE on the next write; write_full() then reports EPIPE and the
	 * helper is handled like any other closed peer.
	 */
	signal(SIGPIPE, SIG_IGN);
	signal(SIGINT, reap);
	signal(SIGTERM, reap);

	parent_pid = getpid();

	/*
	 * Configure passt with the whole network rather than letting it
	 * discover one. It normally reads the host's addresses, routes and
	 * /etc/resolv.conf, and on Android all three are unavailable to an
	 * ordinary uid: netlink link enumeration may be denied outright (passt
	 * reports "Invalid interface name wlan0: Permission denied"), the
	 * routing table is per-uid so several default routes are visible, and
	 * there is no resolv.conf at all.
	 *
	 * Configured explicitly, passt needs none of that for IPv4. It still
	 * reaches the network the only way an unprivileged process can, by
	 * opening ordinary sockets, which is exactly what makes this work
	 * without root.
	 */
	v4_extra[n4++] = "-4";
	v4_extra[n4++] = "-a"; v4_extra[n4++] = addr;
	v4_extra[n4++] = "-n"; v4_extra[n4++] = mask;
	v4_extra[n4++] = "-g"; v4_extra[n4++] = gw;
	if (dns) { v4_extra[n4++] = "-D"; v4_extra[n4++] = dns; }
	else if (dns_disabled) { v4_extra[n4++] = "-D"; v4_extra[n4++] = "none"; }
	if (iface) { v4_extra[n4++] = "-i"; v4_extra[n4++] = iface; }
	v4_extra[n4] = NULL;

	/*
	 * The IPv6 helper is IPv6-only: -6 is passt's way of saying "no IPv4".
	 * With passt-android's prepatched-IPv6 support this configures the
	 * address locally from -a/-g/-D without touching netlink; an older
	 * passt needs netlink here and exits (see the header comment).
	 */
	if (ipv6) {
		v6_extra[n6++] = "-6";
		v6_extra[n6++] = "-a"; v6_extra[n6++] = ipv6_addr;
		v6_extra[n6++] = "-g"; v6_extra[n6++] = ipv6_gw;
		if (ipv6_dns) {
			v6_extra[n6++] = "-D"; v6_extra[n6++] = ipv6_dns;
		} else if (ipv6_dns_disabled) {
			v6_extra[n6++] = "-D"; v6_extra[n6++] = "none";
		}
		if (iface) { v6_extra[n6++] = "-i"; v6_extra[n6++] = iface; }
		v6_extra[n6] = NULL;
	}

	/* The IPv4 helper: the child closes the guest end of the pair too. */
	close_child[0] = uml_sp[0];
	close_child[1] = uml_sp[1];
	close_child[2] = v4_sp[1];
	close_child[3] = ipv6 ? v6_sp[0] : -1;
	close_child[4] = ipv6 ? v6_sp[1] : -1;
	pid = spawn_helper(passt_path, "IPv4", v4_sp[0], v4_sp[1],
			   close_child, ipv6 ? 5 : 3, v4_extra, true);
	if (pid < 0)
		return 1;

	if (ipv6) {
		close_child[0] = uml_sp[0];
		close_child[1] = uml_sp[1];
		close_child[2] = v4_sp[1];
		close_child[3] = v6_sp[1];
		pid = spawn_helper(passt_path, "IPv6", v6_sp[0], v6_sp[1],
				   close_child, 4, v6_extra, false);
		if (pid < 0) {
			/* IPv4 still works; the guest just gets no IPv6. */
			fprintf(stderr, "umnet: cannot start the IPv6 helper\n");
		} else {
			/*
			 * passt parses its configuration before it reaches its
			 * event loop (an older build also opens netlink for
			 * IPv6), so a failure happens in milliseconds.  Give it
			 * that moment, then decide whether this run has IPv6.
			 */
			usleep(200000);
			if (waitpid(pid, &status, WNOHANG) == pid) {
				helpers[1].pid = 0;
				helpers[1].alive = false;
				close(helpers[1].fd);
				helpers[1].fd = -1;
				fprintf(stderr,
					"umnet: ipv6 helper exited (%d); continuing without IPv6\n",
					WIFEXITED(status) ?
					WEXITSTATUS(status) : -1);
			}
		}
	}

	/*
	 * The IPv4 helper is required: if it died before reaching its event loop
	 * (bad --passt path, or passt rejecting an argument), there is no link and
	 * no amount of pumping will create one.  Say so now instead of entering the
	 * pump and waiting for a kernel that booted with nothing behind it.
	 */
	usleep(200000);
	if (waitpid(helpers[0].pid, &status, WNOHANG) == helpers[0].pid) {
		fprintf(stderr, "umnet: passt exited (%d) before serving the link\n",
			WIFEXITED(status) ? WEXITSTATUS(status) : -1);
		helpers[0].pid = 0;
		helpers[0].alive = false;
		close(helpers[0].fd);
		helpers[0].fd = -1;
		return 1;
	}

	if (passt_pid_file) {
		char pidbuf[16];
		int n = snprintf(pidbuf, sizeof(pidbuf), "%d\n",
				 (int)helpers[0].pid);
		int fd = open(passt_pid_file,
			      O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);

		/*
		 * A failure here is a failure to be able to signal the helper
		 * later, so say so rather than leaving a caller that asked for
		 * the file to guess why it is empty.  It is not fatal to the
		 * run: the guest's network does not depend on it.
		 */
		if (fd < 0 || write(fd, pidbuf, n) != n)
			fprintf(stderr, "umnet: cannot write %s: %s\n",
				passt_pid_file, strerror(errno));
		if (fd >= 0)
			close(fd);
	}

	uml_pid = fork();
	uml_pid_global = uml_pid;
	if (uml_pid < 0) {
		/* The helpers are already running and have no PDEATHSIG of their
		 * own (only the UML child gets one), so leaving them here would
		 * keep the socket -- and the link -- open after umnet is gone. */
		perror("umnet: fork");
		for (i = 0; i < nhelpers; i++) {
			if (helpers[i].pid > 0) {
				kill(helpers[i].pid, SIGTERM);
				(void)waitpid(helpers[i].pid, NULL, 0);
			}
		}
		return 1;
	}
	if (uml_pid == 0) {
		close(uml_sp[1]);
		/*
		 * The kernel must not hold the helpers' stream ends: a passt that
		 * exits would then never look closed to the pump, because this
		 * process still holds a copy of the descriptor.  (The kernel
		 * inherits the fds across exec otherwise.)
		 */
		for (i = 0; i < nhelpers; i++)
			if (helpers[i].fd >= 0)
				close(helpers[i].fd);
		/*
		 * Die with our parent.  A supervisor that reaps umnet with
		 * SIGKILL cannot be helped by the signal handler above -- the
		 * handler never runs -- so tie the kernel's life to ours in the
		 * kernel instead.  Without this a killed bridge leaves a booted
		 * guest running with a socket nobody pumps.
		 *
		 * Deliberately no setpgid() here: the kernel inherits our
		 * session and controlling terminal, and putting it in a fresh
		 * group detaches the console it was given, so the guest stops
		 * booting.  PDEATHSIG covers the kill case without that.
		 */
		prctl(PR_SET_PDEATHSIG, SIGKILL);
		if (getppid() != parent_pid)
			_exit(127);

		/* The descriptor has to survive exec for UML to use it. */
		if (fcntl(uml_sp[0], F_SETFD, 0) < 0) {
			perror("umnet: fcntl");
			_exit(1);
		}

		/*
		 * The Mac is pasted into a comma-separated kernel vector, so a
		 * comma (or anything that ends the parameter) would add parameters
		 * of its own.  Reject rather than truncate: a silently wrong vector
		 * is worse than a refused start.
		 */
		if (strchr(mac, ',') || strchr(mac, ' ')) {
			dprintf(STDERR_FILENO,
				"umnet: --mac must not contain a comma or space\n");
			_exit(1);
		}
		if (snprintf(vecarg, sizeof(vecarg),
			     "vec0:transport=fd,fd=%d,mac=%s", uml_sp[0], mac) >=
		    (int)sizeof(vecarg)) {
			dprintf(STDERR_FILENO, "umnet: --mac is too long\n");
			_exit(1);
		}

		child = calloc(argc - umlarg + 2, sizeof(char *));
		if (!child)
			_exit(1);
		for (i = umlarg; i < argc; i++)
			child[i - umlarg] = argv[i];
		child[argc - umlarg] = vecarg;

		execvp(child[0], child);
		perror("umnet: exec uml");
		_exit(127);
	}

	close(uml_sp[0]);

	/*
	 * Pump frames until the guest goes away, not merely until the link
	 * goes quiet.  When UML powers off it closes its end of the datagram
	 * pair, but passt holds its own end open indefinitely: it has no way
	 * to know the guest is gone, so the pump would block in poll() forever
	 * and umnet would never reap the kernel or report its status.  Wait on
	 * the kernel as well, so a guest that exits cleanly ends the run.
	 */
	kernel_status = 0;
	kernel_reaped = false;
	for (;;) {
		pid_t done = waitpid(uml_pid, &kernel_status, WNOHANG);

		if (done == uml_pid) {
			kernel_reaped = true;
			break;
		}
		if (done < 0 && errno != EINTR)
			break;
		if (pump_once(uml_sp[1], 200) < 0)
			break;
	}

	/*
	 * The guest is gone or the link broke; take the helpers with it.
	 *
	 * Report how the kernel died. This was WNOHANG with a discarded status
	 * and an unconditional "return 0", which made every possible failure --
	 * a kernel killed by a signal, a kernel that panicked, a kernel that
	 * never started -- reach the caller as "exited, status 0". Inside an
	 * Android app, where a sandbox can kill the process outright and there
	 * is no shell to ask, that is the difference between a diagnosis and a
	 * guess.
	 */
	for (i = 0; i < nhelpers; i++) {
		if (helpers[i].pid > 0 && helpers[i].alive)
			kill(helpers[i].pid, SIGTERM);
	}

	if (!kernel_reaped && waitpid(uml_pid, &kernel_status, 0) < 0) {
		perror("umnet: waitpid");
		rc = 1;
	} else if (WIFSIGNALED(kernel_status)) {
		fprintf(stderr, "umnet: kernel killed by signal %d (%s)%s\n",
			WTERMSIG(kernel_status), strsignal(WTERMSIG(kernel_status)),
			WCOREDUMP(kernel_status) ? ", core dumped" : "");
		rc = 128 + WTERMSIG(kernel_status);
	} else if (WIFEXITED(kernel_status)) {
		rc = WEXITSTATUS(kernel_status);
		if (rc)
			fprintf(stderr, "umnet: kernel exited %d\n", rc);
	}

	for (i = 0; i < nhelpers; i++) {
		if (helpers[i].pid <= 0)
			continue;
		/*
		 * Bounded reap.  A helper that does not die on SIGTERM (SIGSTOPped,
		 * or wedged in the kernel) must not hold up the exit status that
		 * the supervisor is waiting for.  Give it a moment, then SIGKILL.
		 */
		if (reap_helper(helpers[i].pid, &status) <= 0)
			continue;
		if (!WIFSIGNALED(status) && !WIFEXITED(status))
			continue;
		if (helpers[i].required) {
			/* Keep the wording the gates and evidence grep for. */
			if (WIFSIGNALED(status))
				fprintf(stderr,
					"umnet: passt killed by signal %d (%s)\n",
					WTERMSIG(status),
					strsignal(WTERMSIG(status)));
			else if (WEXITSTATUS(status))
				fprintf(stderr, "umnet: passt exited %d\n",
					WEXITSTATUS(status));
		} else if (WIFSIGNALED(status)) {
			if (verbose)
				fprintf(stderr,
					"umnet: ipv6 passt killed by signal %d\n",
					WTERMSIG(status));
		} else if (WEXITSTATUS(status) && verbose) {
			fprintf(stderr, "umnet: ipv6 passt exited %d\n",
				WEXITSTATUS(status));
		}
	}

	return rc;
}
