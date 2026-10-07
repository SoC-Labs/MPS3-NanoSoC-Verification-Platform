/*
 * test_version.c — host-gcc unit tests for the HARNESS VERSION seam
 * (firmware/platform/mps3_version.h + mps3_version_weak.c).
 *
 * Links ONLY mps3_version_weak.c — no generated identity, by design. That IS
 * the test: the seam's whole contract is that firmware builds and links with
 * NO generated file present (a fresh clone, or exactly this host-gcc harness),
 * reporting an honest "not provisioned" 0.0.0 rather than failing to link or
 * inventing a version. If someone deletes the weak TU, or makes a caller depend
 * on the generated strong override, this binary stops linking and says so.
 *
 * The other half — that scripts/gen_version.py's Python encoder and this
 * header's C macro agree, and that the generated strong override actually wins
 * at link time — is pinned cross-language by
 * tests/firmware_logic/test_version_gen.py (it generates, compiles and RUNS
 * the real generated TU). Neither test alone is sufficient: this one proves the
 * fallback, that one proves the real thing.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../platform/mps3_version.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* ---- the packed encoding (docs/VERSIONING_PLAN.md §3.1) ------------------ */

static void test_worked_example_from_the_plan(void)
{
    /* The single worked example the whole scheme is documented against:
     * v1.4.2 from a CLEAN tree packs to 0x01040200, and the same version from
     * a DIRTY tree sets flags bit0 -> 0x01040201. scripts/gen_version.py's
     * pack_ver32() must produce these exact numbers too (cross-checked in
     * tests/firmware_logic/test_version_gen.py) — one encoding, two languages. */
    CHECK(MPS3_HARNESS_PACK_VER32(1, 4, 2, 0) == 0x01040200u);
    CHECK(MPS3_HARNESS_PACK_VER32(1, 4, 2, MPS3_HARNESS_FLAG_DIRTY) == 0x01040201u);
}

static void test_field_placement(void)
{
    /* Each field occupies its own byte and does not bleed into a neighbour. */
    CHECK(MPS3_HARNESS_PACK_VER32(0xFF, 0, 0, 0) == 0xFF000000u);
    CHECK(MPS3_HARNESS_PACK_VER32(0, 0xFF, 0, 0) == 0x00FF0000u);
    CHECK(MPS3_HARNESS_PACK_VER32(0, 0, 0xFF, 0) == 0x0000FF00u);
    CHECK(MPS3_HARNESS_PACK_VER32(0, 0, 0, 0xFF) == 0x000000FFu);

    /* Out-of-range fields are masked, never allowed to corrupt a neighbouring
     * byte (gen_version.py rejects >255 up front; the macro must not silently
     * smear if a caller hands it one anyway). */
    CHECK(MPS3_HARNESS_PACK_VER32(0x101, 0, 0, 0) == 0x01000000u);
}

static void test_unpack_round_trip(void)
{
    uint32_t v = MPS3_HARNESS_PACK_VER32(1, 4, 2, MPS3_HARNESS_FLAG_DIRTY);

    CHECK(MPS3_HARNESS_VER_MAJOR(v) == 1u);
    CHECK(MPS3_HARNESS_VER_MINOR(v) == 4u);
    CHECK(MPS3_HARNESS_VER_PATCH(v) == 2u);
    CHECK(MPS3_HARNESS_VER_FLAGS(v) == MPS3_HARNESS_FLAG_DIRTY);
    CHECK(MPS3_HARNESS_VER_IS_DIRTY(v));

    uint32_t clean = MPS3_HARNESS_PACK_VER32(255, 255, 255, 0);
    CHECK(MPS3_HARNESS_VER_MAJOR(clean) == 255u);
    CHECK(MPS3_HARNESS_VER_MINOR(clean) == 255u);
    CHECK(MPS3_HARNESS_VER_PATCH(clean) == 255u);
    CHECK(!MPS3_HARNESS_VER_IS_DIRTY(clean));
}

static void test_reserved_flag_bits_do_not_alias_dirty(void)
{
    /* bits[7:1] are reserved. A future flag landing in one of them must not be
     * mistaken for "dirty" — the dirty test is a bit test, not != 0. */
    uint32_t v = MPS3_HARNESS_PACK_VER32(1, 0, 0, 0x02u); /* reserved bit1 only */
    CHECK(!MPS3_HARNESS_VER_IS_DIRTY(v));
    CHECK(MPS3_HARNESS_VER_FLAGS(v) == 0x02u);
}

/* ---- the weak seam ------------------------------------------------------- */

static void test_weak_fallback_is_not_provisioned(void)
{
    /* No generated identity is linked into this binary, so every accessor must
     * return the "not provisioned" answer. 0 / "0.0.0" is deliberately
     * unmistakable: no shipped harness ever legitimately reports 0.0.0, so it
     * reads as "built without the generator", never as "an old release". */
    CHECK(mps3_harness_version() == 0u);
    CHECK(strcmp(mps3_harness_version_str(), "0.0.0") == 0);
    CHECK(strcmp(mps3_harness_git_sha(), "unknown") == 0);
    CHECK(mps3_harness_dirty() == false);
    CHECK(strcmp(mps3_harness_build_date(), "unknown") == 0);
}

static void test_weak_fallback_is_self_consistent(void)
{
    /* The two ways of asking "is this dirty?" must agree even in the fallback:
     * a caller holding only a ver32 unpacks it, a caller holding neither calls
     * the accessor. Both say "no". */
    CHECK(MPS3_HARNESS_VER_IS_DIRTY(mps3_harness_version()) == mps3_harness_dirty());
}

int main(void)
{
    test_worked_example_from_the_plan();
    test_field_placement();
    test_unpack_round_trip();
    test_reserved_flag_bits_do_not_alias_dirty();
    test_weak_fallback_is_not_provisioned();
    test_weak_fallback_is_self_consistent();

    printf("test_version: %d checks passed\n", s_checks);
    return 0;
}
