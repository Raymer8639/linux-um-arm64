/* SPDX-License-Identifier: GPL-2.0 */
/*
 * arch/um/Makefile force-includes <linux/compiler_attributes.h> ahead of the
 * host libc headers for every USER_CFLAGS translation unit.
 *
 * glibc 2.4x defines
 *
 *     __attribute_alloc_size__(params) __attribute__ ((__alloc_size__ params))
 *
 * and expands it at each use site. While the kernel's function-like
 * __alloc_size__ macro is in scope, that expands to a nested __attribute__(()),
 * which is a syntax error: "expected ')' before '__attribute__'".
 *
 * The host libc only needs the bare attribute name, so drop the macro here.
 * Included from USER_CFLAGS right after compiler_attributes.h; kernel-internal
 * translation units never see this header.
 */
#undef __alloc_size__
