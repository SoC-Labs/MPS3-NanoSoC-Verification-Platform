/*
 * mps3_version_weak.c — WEAK fallbacks for the harness-version seam
 * (firmware/platform/mps3_version.h). Committed; hand-written.
 *
 * The real values are baked in at build time by the GENERATED strong override
 * firmware/platform/generated/mps3_version.c (scripts/gen_version.py). This
 * file is what makes that generated file OPTIONAL: link it alone and the
 * firmware still builds, reporting the honest "not provisioned" answer
 * (0.0.0 / sha "unknown" / ver32 0) rather than failing to link or, worse,
 * inventing a version.
 *
 * Exactly the pattern coordinator.c uses for mps3_shell_static_id(): a weak
 * definition returning 0 = "not provisioned", strongly overridden on the real
 * shell by a generated TU. Keeping the fallback in its OWN translation unit
 * (rather than inside a module like coordinator.c) means any host-gcc test can
 * pick up the seam by adding one file to its link line, and nothing else in
 * firmware/ needs to know the version machinery exists.
 *
 * WHY 0 AND NOT A HARD-CODED "1.0.0": a version compiled into a fallback is a
 * version that lies the moment VERSION moves. 0 is unmistakable — no shipped
 * harness ever legitimately reports 0.0.0, so 0 reads as "this image was built
 * without the generator" and not as "this image is old".
 */
#include "mps3_version.h"

__attribute__((weak)) uint32_t mps3_harness_version(void)
{
    return 0u;
}

__attribute__((weak)) const char *mps3_harness_version_str(void)
{
    return "0.0.0";
}

__attribute__((weak)) const char *mps3_harness_git_sha(void)
{
    return "unknown";
}

__attribute__((weak)) bool mps3_harness_dirty(void)
{
    return false;
}

__attribute__((weak)) const char *mps3_harness_build_date(void)
{
    return "unknown";
}
