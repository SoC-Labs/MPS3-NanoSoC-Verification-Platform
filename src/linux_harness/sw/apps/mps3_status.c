/*
 * mps3-status — one-shot boot-status surface (M5 app port).
 *
 * Prints the same status snapshot the CLCD renders, as one flat JSON line —
 * the Linux successor of what the bare-metal harness surfaced (TELEM's honest
 * "no power sensor" + the status screen's fields). LOCAL surface only: the
 * frozen :6900 wire shapes (telemetry/diag) belong to mps3-ctrld.
 *
 *   mps3-status                  # JSON to stdout
 *   mps3-status -o /run/mps3/status.json
 *   mps3-status --text           # the 40x15 screen as ASCII (same frame
 *                                # the panel shows; works with no hardware)
 *
 * MPS3_SYSROOT env re-roots every source file for off-board testing.
 */
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "clcd_core.h"
#include "status_linux.h"

int main(int argc, char **argv)
{
    mps3_status_cfg_t cfg;
    mps3_status_cfg_default(&cfg);
    const char *out_file = NULL;
    const char *board = MPS3_BOARD_NAME;
    int text = 0;

    const char *env_root = getenv("MPS3_SYSROOT");
    if (env_root)
        cfg.root = env_root;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "-n") && i + 1 < argc)      cfg.netdev = argv[++i];
        else if (!strcmp(argv[i], "-i") && i + 1 < argc) cfg.static_id_file = argv[++i];
        else if (!strcmp(argv[i], "-b") && i + 1 < argc) board = argv[++i];
        else if (!strcmp(argv[i], "-o") && i + 1 < argc) out_file = argv[++i];
        else if (!strcmp(argv[i], "--text"))             text = 1;
        else {
            fprintf(stderr,
                "usage: %s [-n iface] [-i static_id_file] [-b board] "
                "[-o file] [--text]\n", argv[0]);
            return 2;
        }
    }

    clcd_status_t st;
    mps3_status_collect(&cfg, &st);
    snprintf(st.board_name, sizeof(st.board_name), "%s", board);

    if (text) {
        char frame[CLCD_NCELLS];
        uint8_t inv[CLCD_ROWS];
        clcd_build_frame(&st, 0, frame, inv);
        for (unsigned r = 0; r < CLCD_ROWS; r++)
            printf("%.*s%s\n", (int)CLCD_COLS, frame + r * CLCD_COLS,
                   inv[r] ? "   <inv>" : "");
        return 0;
    }

    char jb[1024];
    mps3_status_json(&st, jb, sizeof(jb));

    if (out_file) {
        char tmp[300];
        snprintf(tmp, sizeof(tmp), "%s.tmp", out_file);
        FILE *f = fopen(tmp, "w");
        if (!f) {
            perror("mps3-status: open");
            return 1;
        }
        fprintf(f, "%s\n", jb);
        fclose(f);
        if (rename(tmp, out_file) != 0) {
            perror("mps3-status: rename");
            return 1;
        }
    } else {
        printf("%s\n", jb);
    }
    return 0;
}
