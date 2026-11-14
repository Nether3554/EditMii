/*
 * EditMii - Nintendo 3DS Mii database editor (native homebrew)
 *
 * Edits the system Mii database (CFL_DB.dat and CFL_OldDB.dat) stored in the
 * shared extdata 0xF000000B on NAND:
 *   nand:/data/<ID0>/extdata/00048000/f000000b/00000000/00000005 and 00000006
 *
 * The files are opened through the system file service (FS), which hands out
 * the decoded data and recalculates the DIFF hash tree + AES-CMAC itself when
 * the file is written. No GodMode9 and no PC are needed.
 *
 * Mii Maker records (your own Miis) are NEVER modified.
 */
#include <3ds.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "mii_db.h"

#define APP_VERSION "2.1"

/* shared extdata 0xF000000B  ->  media NAND, id low 0xF000000B, id high 0x00048000 */
#define EXTDATA_ID_LOW   0xF000000Bu
#define EXTDATA_ID_HIGH  0x00048000u
#define ARCHIVE_ID_SHARED_EXTDATA ((FS_ArchiveID)0x00000007)

#define ERR_BADSIZE   ((Result)0xE0000001)
#define ERR_BADFORMAT ((Result)0xE0000002)
#define ERR_SHORT     ((Result)0xE0000003)
#define ERR_VERIFY    ((Result)0xE0000004)
#define ERR_NOMEM     ((Result)0xE0000005)

#define C_RESET  "\x1b[0m"
#define C_RED    "\x1b[31;1m"
#define C_GREEN  "\x1b[32;1m"
#define C_YELLOW "\x1b[33;1m"
#define C_CYAN   "\x1b[36;1m"
#define C_GRAY   "\x1b[30;1m"

enum { KIND_HIST = 0, KIND_INV = 1, KIND_MAKER = 2 };
enum { SCR_MAIN, SCR_LIST };

#define LIST_ROWS 24

/* ------------------------------------------------------------------ */
/* state                                                              */
/* ------------------------------------------------------------------ */
typedef struct {
    const char *label;          /* "CFL_DB.dat" */
    char        path[48];       /* path inside the archive, e.g. "/CFL_DB.dat" */
    uint8_t    *work;           /* current (edited) data */
    uint8_t    *orig;           /* data as it is on NAND right now */
    uint8_t     maker_ref[MAKER_END];
    int         loaded;
    int         changed;
    Result      err;
    char        problems[256];
    char        detail[200];
} DbFile;

typedef struct {
    uint16_t idx;
    uint8_t  mark;
    char     name[12];
    char     c2[20];
    char     date[18];
} Row;

static PrintConsole g_top, g_bot;
static FS_Archive   g_arch;
static int          g_arch_ok = 0;
static Result       g_arch_err = 0;

static DbFile g_db[2] = { { .label = "CFL_DB.dat" }, { .label = "CFL_OldDB.dat" } };
static uint8_t *g_tmpbuf = NULL;

static Row  g_rows[NODE_N];
static int  g_nrows = 0;
static uint16_t g_idxtmp[NODE_N];

static int g_screen = SCR_MAIN;
static int g_kind = KIND_HIST;
static int g_file = 0;
static int g_cursor = 0, g_scroll = 0;
static int g_menu = 0;
static int g_apply_both = 1;
static int g_dirty = 1;

static int  g_tried = 0;          /* user already asked to open the NAND archive */

static char g_msg[240] = "";
static int  g_msg_level = 0;      /* 0 info, 1 error, 2 ok */

/* root listing of the archive (fallback when file names differ) */
static char g_root[16][40];
static int  g_nroot = 0, g_root_scanned = 0;

/* ------------------------------------------------------------------ */
/* custom startup: only the services this program needs.             */
/* ------------------------------------------------------------------ */
void __appInit(void);
void __appExit(void);

void __appInit(void)
{
    srvInit();
    aptInit();
    hidInit();
    fsInit();
}

void __appExit(void)
{
    fsExit();
    hidExit();
    aptExit();
    srvExit();
}


/* ------------------------------------------------------------------ */
/* small helpers                                                      */
/* ------------------------------------------------------------------ */
static void set_msg(int level, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(g_msg, sizeof g_msg, fmt, ap);
    va_end(ap);
    g_msg_level = level;
    g_dirty = 1;
}

static int is_custom_err(Result rc) { return (((u32)rc) & 0xFFFF0000u) == 0xE0000000u; }

static const char *err_text(Result rc)
{
    switch (rc) {
    case ERR_BADSIZE:   return "unexpected file size";
    case ERR_BADFORMAT: return "not a Mii database";
    case ERR_SHORT:     return "short read/write";
    case ERR_VERIFY:    return "read-back verification failed";
    case ERR_NOMEM:     return "out of memory";
    default:            return "";
    }
}

static void present(void)
{
    gfxFlushBuffers();
    gfxSwapBuffers();
    gspWaitForVBlank();
}

static void lower_copy(char *dst, const char *src, size_t n)
{
    size_t i = 0;
    for (; src[i] && i + 1 < n; i++)
        dst[i] = (src[i] >= 'A' && src[i] <= 'Z') ? (char)(src[i] + 32) : src[i];
    dst[i] = 0;
}

/* repeat for the D-Pad: initial delay ~18 frames, then every 3rd frame */
static u32 key_repeat(u32 down, u32 held)
{
    static int cnt = 0;
    const u32 dirs = KEY_DUP | KEY_DDOWN | KEY_DLEFT | KEY_DRIGHT;
    u32 h = held & dirs;
    if (down & dirs) { cnt = 0; return 0; }
    if (h) {
        cnt++;
        if (cnt > 18 && (cnt % 3) == 0) return h;
    } else {
        cnt = 0;
    }
    return 0;
}

/* modal Yes/No on the bottom screen. A = yes, B = no */
static int confirm(const char *l1, const char *l2, const char *l3)
{
    consoleSelect(&g_bot);
    consoleClear();
    printf("\n " C_YELLOW "Please confirm" C_RESET "\n\n");
    if (l1 && *l1) printf(" %s\n\n", l1);
    if (l2 && *l2) printf(" %s\n\n", l2);
    if (l3 && *l3) printf(" %s\n\n", l3);
    printf("\n " C_GREEN "A" C_RESET ": Yes        " C_RED "B" C_RESET ": No\n");
    present();
    while (aptMainLoop()) {
        hidScanInput();
        u32 k = hidKeysDown();
        if (k & KEY_A) { g_dirty = 1; return 1; }
        if (k & KEY_B) { g_dirty = 1; return 0; }
        present();
    }
    return 0;
}

/* ------------------------------------------------------------------ */
/* NAND (shared extdata) access                                       */
/* ------------------------------------------------------------------ */
static Result arch_open(void)
{
    u32 path[3] = { MEDIATYPE_NAND, EXTDATA_ID_LOW, EXTDATA_ID_HIGH };
    FS_Path p = { PATH_BINARY, sizeof(path), path };
    Result rc = FSUSER_OpenArchive(&g_arch, ARCHIVE_ID_SHARED_EXTDATA, p);
    g_arch_ok = R_SUCCEEDED(rc);
    g_arch_err = rc;
    return rc;
}

static void arch_close(void)
{
    if (g_arch_ok) FSUSER_CloseArchive(g_arch);
    g_arch_ok = 0;
}

static Result nand_read(const char *path, uint8_t *buf)
{
    Handle fh;
    Result rc = FSUSER_OpenFile(&fh, g_arch, fsMakePath(PATH_ASCII, path), FS_OPEN_READ, 0);
    if (R_FAILED(rc)) return rc;
    u64 sz = 0;
    u32 br = 0;
    rc = FSFILE_GetSize(fh, &sz);
    if (R_SUCCEEDED(rc) && sz != DB_SIZE) rc = ERR_BADSIZE;
    if (R_SUCCEEDED(rc)) rc = FSFILE_Read(fh, &br, 0, buf, DB_SIZE);
    FSFILE_Close(fh);
    if (R_SUCCEEDED(rc) && br != DB_SIZE) rc = ERR_SHORT;
    if (R_SUCCEEDED(rc) && db_validate(buf, DB_SIZE) != 0) rc = ERR_BADFORMAT;
    return rc;
}

static Result nand_write(const char *path, const uint8_t *buf)
{
    Handle fh;
    Result rc = FSUSER_OpenFile(&fh, g_arch, fsMakePath(PATH_ASCII, path), FS_OPEN_WRITE, 0);
    if (R_FAILED(rc)) return rc;
    u32 bw = 0;
    rc = FSFILE_Write(fh, &bw, 0, buf, DB_SIZE, FS_WRITE_FLUSH);
    if (R_SUCCEEDED(rc) && bw != DB_SIZE) rc = ERR_SHORT;
    if (R_SUCCEEDED(rc)) rc = FSFILE_Flush(fh);
    Result rc2 = FSFILE_Close(fh);
    if (R_SUCCEEDED(rc)) rc = rc2;
    return rc;
}

static void scan_root(void)
{
    static FS_DirectoryEntry ents[8];
    Handle dh;
    g_nroot = 0;
    g_root_scanned = 1;
    if (R_FAILED(FSUSER_OpenDirectory(&dh, g_arch, fsMakePath(PATH_ASCII, "/")))) return;
    while (g_nroot < 16) {
        u32 nread = 0;
        if (R_FAILED(FSDIR_Read(dh, &nread, 8, ents)) || nread == 0) break;
        for (u32 i = 0; i < nread && g_nroot < 16; i++) {
            if (ents[i].attributes & FS_ATTRIBUTE_DIRECTORY) continue;
            char *o = g_root[g_nroot];
            size_t n = 0;
            for (int j = 0; j < 38 && ents[i].name[j]; j++)
                o[n++] = ents[i].name[j] < 0x80 ? (char)ents[i].name[j] : '?';
            o[n] = 0;
            g_nroot++;
        }
    }
    FSDIR_Close(dh);
}

static void load_db(int idx)
{
    DbFile *d = &g_db[idx];
    d->loaded = 0;
    d->changed = 0;
    d->problems[0] = 0;
    d->detail[0] = 0;
    snprintf(d->path, sizeof d->path, "/%s", d->label);

    Result rc = nand_read(d->path, d->work);
    if (R_FAILED(rc) && !is_custom_err(rc)) {
        Result first = rc;
        if (!g_root_scanned) scan_root();
        for (int i = 0; i < g_nroot; i++) {
            char low[40];
            lower_copy(low, g_root[i], sizeof low);
            int isold = strstr(low, "old") != NULL;
            if (isold != (idx == 1)) continue;
            char tp[48];
            snprintf(tp, sizeof tp, "/%s", g_root[i]);
            Result r2 = nand_read(tp, d->work);
            if (R_SUCCEEDED(r2)) {
                strcpy(d->path, tp);
                rc = r2;
                break;
            }
        }
        if (R_FAILED(rc)) {
            rc = first;
            size_t n = 0;
            n += (size_t)snprintf(d->detail, sizeof d->detail, "Files in archive root:");
            for (int i = 0; i < g_nroot && n < sizeof d->detail - 24; i++)
                n += (size_t)snprintf(d->detail + n, sizeof d->detail - n, " %s", g_root[i]);
            if (!g_nroot) snprintf(d->detail + n, sizeof d->detail - n, " (none / unreadable)");
        }
    }
    d->err = rc;
    if (R_FAILED(rc)) return;

    memcpy(d->orig, d->work, DB_SIZE);
    memcpy(d->maker_ref, d->work, MAKER_END);
    d->loaded = 1;
    db_check(d->work, d->problems, sizeof d->problems);
}

static void load_all(void)
{
    g_root_scanned = 0;
    for (int i = 0; i < 2; i++) load_db(i);
}

static int any_changes(void)
{
    return (g_db[0].loaded && g_db[0].changed) || (g_db[1].loaded && g_db[1].changed);
}

/* ------------------------------------------------------------------ */
/* list model                                                         */
/* ------------------------------------------------------------------ */
static const char *kind_title(int k)
{
    return k == KIND_HIST ? "Encounter history" : k == KIND_INV ? "Mii Plaza invitations" : "Mii Maker (read-only)";
}

static void rebuild_rows(void)
{
    g_nrows = 0;
    DbFile *d = &g_db[g_file];
    if (!d->loaded) return;
    const uint8_t *p = d->work;

    if (g_kind == KIND_HIST) {
        int n = db_hist_used_list(p, g_idxtmp);
        for (int k = 0; k < n; k++) {
            Row *r = &g_rows[g_nrows++];
            const uint8_t *rec = db_hist_rec(p, g_idxtmp[k]);
            r->idx = g_idxtmp[k];
            r->mark = 0;
            mii_name_ascii(rec + 0x1A, r->name);
            fmt_mac(rec + 0x10, r->c2);
            fmt_date_2000(rd32le(rec + 0x48), r->date, 0);
        }
    } else if (g_kind == KIND_INV) {
        uint32_t n = db_inv_count(p);
        if (n > INV_N) n = INV_N;
        for (uint32_t i = 0; i < n; i++) {
            Row *r = &g_rows[g_nrows++];
            const uint8_t *rec = db_inv_rec(p, (int)i);
            r->idx = (uint16_t)i;
            r->mark = 0;
            mii_name_ascii(rec + 0x1A, r->name);
            fmt_mac(rec + 0x10, r->c2);
            fmt_mii_created(rec, r->date);
        }
    } else {
        for (int k = 0; k < MM_N; k++) {
            if (!db_maker_valid(p, k)) continue;
            Row *r = &g_rows[g_nrows++];
            const uint8_t *rec = db_maker_rec(p, k);
            r->idx = (uint16_t)k;
            r->mark = 0;
            mii_name_ascii(rec + 0x1A, r->name);
            mii_name_ascii(rec + 0x48, r->c2);
            fmt_mii_created(rec, r->date);
        }
    }
    if (g_cursor >= g_nrows) g_cursor = g_nrows ? g_nrows - 1 : 0;
    if (g_scroll > g_cursor) g_scroll = g_cursor;
    g_dirty = 1;
}

static int count_marked(void)
{
    int n = 0;
    for (int i = 0; i < g_nrows; i++) n += g_rows[i].mark;
    return n;
}

static void enter_list(int kind)
{
    int f = -1;
    if (g_db[0].loaded) f = 0; else if (g_db[1].loaded) f = 1;
    if (f < 0) { set_msg(1, "No database is loaded."); return; }
    g_kind = kind;
    if (!g_db[g_file].loaded) g_file = f;
    g_cursor = g_scroll = 0;
    g_screen = SCR_LIST;
    rebuild_rows();
}

/* ------------------------------------------------------------------ */
/* actions                                                            */
/* ------------------------------------------------------------------ */
static void mark_by_name(void)
{
    SwkbdState kb;
    char in[64] = "";
    swkbdInit(&kb, SWKBD_TYPE_NORMAL, 2, 10);
    swkbdSetHintText(&kb, "Part of a Mii name");
    swkbdSetButton(&kb, SWKBD_BUTTON_LEFT, "Cancel", false);
    swkbdSetButton(&kb, SWKBD_BUTTON_RIGHT, "Mark", true);
    SwkbdButton b = swkbdInputText(&kb, in, sizeof in);
    g_dirty = 1;
    if (b != SWKBD_BUTTON_RIGHT || !in[0]) return;

    char folded[64], needle[64];
    fold_utf8_ascii(in, folded, sizeof folded);
    lower_copy(needle, folded, sizeof needle);
    int n = 0;
    for (int i = 0; i < g_nrows; i++) {
        char low[24];
        lower_copy(low, g_rows[i].name, sizeof low);
        if (strstr(low, needle)) { g_rows[i].mark = 1; n++; }
    }
    set_msg(0, "Marked %d entries matching \"%s\".", n, needle);
}

static void do_delete_marked(void)
{
    static uint8_t drop[2][NODE_N];
    static uint8_t keys[NODE_N][10];
    if (g_kind == KIND_MAKER) return;
    int nm = count_marked();
    if (!nm) { set_msg(1, "Nothing is marked. Press A to mark entries."); return; }

    int other = 1 - g_file;
    int both = g_apply_both && g_db[other].loaded;

    char l1[100], l2[100];
    snprintf(l1, sizeof l1, "Delete %d marked entries from \"%s\"?", nm, kind_title(g_kind));
    if (both) snprintf(l2, sizeof l2, "Applies to BOTH files (matched by ID+MAC).");
    else snprintf(l2, sizeof l2, "Applies to %s only.", g_db[g_file].label);
    if (!confirm(l1, l2, "Mii Maker stays untouched. Nothing is written to NAND until you use \"Save changes\".")) return;

    memset(drop, 0, sizeof drop);
    int nk = 0;
    DbFile *cur = &g_db[g_file];
    for (int i = 0; i < g_nrows; i++) {
        if (!g_rows[i].mark) continue;
        int idx = g_rows[i].idx;
        drop[g_file][idx] = 1;
        const uint8_t *key = (g_kind == KIND_HIST) ? db_node(cur->work, idx) : db_inv_rec(cur->work, idx) + 0x0C;
        memcpy(keys[nk++], key, 10);
    }

    if (both) {
        DbFile *o = &g_db[other];
        int count = (g_kind == KIND_HIST) ? NODE_N : (int)db_inv_count(o->work);
        if (count > (g_kind == KIND_HIST ? NODE_N : INV_N)) count = INV_N;
        for (int j = 0; j < count; j++) {
            const uint8_t *key;
            if (g_kind == KIND_HIST) {
                if (!db_node_used(o->work, j)) continue;
                key = db_node(o->work, j);
            } else {
                key = db_inv_rec(o->work, j) + 0x0C;
            }
            for (int k = 0; k < nk; k++) {
                if (memcmp(keys[k], key, 10) == 0) { drop[other][j] = 1; break; }
            }
        }
    }

    int total = 0, fail = 0;
    for (int f = 0; f < 2; f++) {
        if (f != g_file && !both) continue;
        DbFile *d = &g_db[f];
        int r = (g_kind == KIND_HIST) ? db_delete_history(d->work, drop[f]) : db_delete_invitations(d->work, drop[f]);
        if (r < 0) fail = 1;
        else if (r > 0) { total += r; d->changed = 1; }
    }
    rebuild_rows();
    if (fail) set_msg(1, "Deleted %d, but a list in one file is corrupted and was skipped.", total);
    else set_msg(2, "Deleted %d entries. Use \"Save changes\" in the main menu to write them.", total);
}

static void do_wipe(void)
{
    if (!g_db[0].loaded && !g_db[1].loaded) { set_msg(1, "No database is loaded."); return; }
    if (!confirm("Delete ALL encounter history and ALL Mii Plaza invitations in every loaded file?",
                 "Mii Maker (your own Miis) stays untouched.",
                 "Nothing is written to NAND until you use \"Save changes\".")) return;
    int total = 0;
    for (int f = 0; f < 2; f++) {
        DbFile *d = &g_db[f];
        if (!d->loaded) continue;
        int n = db_hist_used_list(d->work, g_idxtmp) + (int)db_inv_count(d->work);
        db_clear_history(d->work);
        db_clear_invitations(d->work);
        if (n) { d->changed = 1; total += n; }
    }
    set_msg(2, "Cleared %d entries. Use \"Save changes\" to write them.", total);
    rebuild_rows();
}

static int verify_written(DbFile *d)
{
    Result rc = nand_read(d->path, g_tmpbuf);
    if (R_FAILED(rc)) return 0;
    return memcmp(g_tmpbuf, d->work, DB_SIZE) == 0;
}

static void do_save(void)
{
    if (!any_changes()) { set_msg(0, "No changes to save."); return; }

    char probs[256], line[300];
    for (int f = 0; f < 2; f++) {
        DbFile *d = &g_db[f];
        if (!d->loaded || !d->changed) continue;
        db_fix_crc(d->work);
        if (memcmp(d->work, d->maker_ref, MAKER_END) != 0) {
            set_msg(1, "%s: Mii Maker section differs from the reference. Aborted, nothing written.", d->label);
            return;
        }
        if (db_check(d->work, probs, sizeof probs) > 0) {
            snprintf(line, sizeof line, "%s: %s", d->label, probs);
            if (!confirm("Self-check found problems:", line, "Save anyway?")) {
                set_msg(1, "Save cancelled.");
                return;
            }
        }
    }
    if (!confirm("Write the changes to the system Mii database on NAND?",
                 "No backup is made by this program. Make sure you have one (e.g. from GodMode9).",
                 "Reboot the console afterwards so every system module reloads the data.")) return;

    int saved = 0;
    for (int f = 0; f < 2; f++) {
        DbFile *d = &g_db[f];
        if (!d->loaded || !d->changed) continue;
        Result rc = nand_write(d->path, d->work);
        if (R_FAILED(rc)) {
            set_msg(1, "Writing %s failed: 0x%08lX %s", d->label, (unsigned long)rc, err_text(rc));
            return;
        }
        if (!verify_written(d)) {
            set_msg(1, "%s: read-back differs from what was written!", d->label);
            return;
        }
        memcpy(d->orig, d->work, DB_SIZE);
        d->changed = 0;
        db_check(d->work, d->problems, sizeof d->problems);
        saved++;
    }
    set_msg(2, "Saved %d file(s) and verified. Please reboot the console.", saved);
}

static void do_load(void)
{
    if (any_changes() && !confirm("Discard all unsaved changes and reload from NAND?", NULL, NULL)) return;
    g_tried = 1;
    if (!g_arch_ok) {
        Result rc = arch_open();
        if (R_FAILED(rc)) {
            g_db[0].err = g_db[1].err = rc;
            set_msg(1, "Cannot open shared extdata 0xF000000B (0x%08lX).", (unsigned long)rc);
            return;
        }
    }
    load_all();
    for (int i = 0; i < 2; i++)
    g_screen = SCR_MAIN;
    if (g_db[0].loaded || g_db[1].loaded) set_msg(2, "Databases loaded from NAND.");
    else set_msg(1, "Could not load the Mii databases.");
}

/* ------------------------------------------------------------------ */
/* drawing                                                            */
/* ------------------------------------------------------------------ */
static const char *MENU_ITEMS[] = {
    "Load / reload databases from NAND",
    "Encounter history (StreetPass etc.)",
    "Mii Plaza invitations",
    "Mii Maker (read-only view)",
    "Wipe history + invitations",
    "Save changes to NAND",
    "Exit",
};
#define MENU_N 7

static void draw_main(void)
{
    consoleSelect(&g_top);
    consoleClear();
    printf(C_CYAN " EditMii v" APP_VERSION C_RESET " - 3DS Mii database editor\n");
    printf(" Shared extdata 0xF000000B (NAND)\n");
    printf(" ------------------------------------------------\n");
    for (int f = 0; f < 2; f++) {
        DbFile *d = &g_db[f];
        if (d->loaded) {
            int nh = db_hist_used_list(d->work, g_idxtmp);
            printf(" %-13s %s  M:%3d H:%4d I:%3d %s\n", d->label,
                   d->problems[0] ? C_YELLOW "WARN" C_RESET : C_GREEN " OK " C_RESET,
                   db_maker_count(d->work), nh, (int)db_inv_count(d->work),
                   d->changed ? C_YELLOW "*" C_RESET : "");
        } else if (!g_tried) {
            printf(" %-13s " C_YELLOW "not loaded yet" C_RESET "\n", d->label);
        } else {
            printf(" %-13s " C_RED "NOT LOADED" C_RESET " (0x%08lX)\n", d->label, (unsigned long)d->err);
        }
    }
    printf(C_GRAY " M=Mii Maker  H=history  I=invitations  *=unsaved\n" C_RESET);
    printf(" ------------------------------------------------\n\n");
    for (int i = 0; i < MENU_N; i++) {
        printf(" %s %s%s\n", i == g_menu ? C_GREEN ">" C_RESET : " ",
               i == g_menu ? C_GREEN : "", MENU_ITEMS[i]);
        if (i == g_menu) printf(C_RESET);
    }
}

static void draw_main_bottom(void)
{
    consoleSelect(&g_bot);
    consoleClear();
    printf("\n " C_CYAN "Controls" C_RESET "\n");
    printf(" Up/Down: move    A: select\n START: exit\n\n");
    printf(" Mii Maker (your own Miis) is never\n modified by this program.\n");
    printf(" Hashes and CMAC are handled by the\n system, no GodMode9 needed.\n\n");
    if (g_msg[0])
        printf(" %s%s" C_RESET "\n\n", g_msg_level == 1 ? C_RED : g_msg_level == 2 ? C_GREEN : C_YELLOW, g_msg);
    for (int f = 0; f < 2; f++) {
        DbFile *d = &g_db[f];
        if (!d->loaded && g_tried) {
            printf(" " C_RED "%s:" C_RESET " %s\n", d->label, err_text(d->err));
            if (!g_arch_ok)
                printf(" Archive open error 0x%08lX.\n Needs Luma3DS and enough FS rights.\n", (unsigned long)g_arch_err);
            if (d->detail[0]) printf(" %s\n", d->detail);
        } else if (d->problems[0]) {
            printf(" " C_YELLOW "%s:" C_RESET " %s\n", d->label, d->problems);
        }
    }
}

static void draw_list(void)
{
    consoleSelect(&g_top);
    consoleClear();
    DbFile *d = &g_db[g_file];
    printf(C_CYAN " %s" C_RESET " - %s\n", kind_title(g_kind), d->label);
    int editable = g_kind != KIND_MAKER;
    printf(" Entries: %d   Marked: %d   Both files: %s\n", g_nrows, count_marked(),
           editable ? (g_apply_both ? "ON" : "OFF") : "-");
    printf(" ------------------------------------------------\n");
    printf(" %-3s %-4s %-10s %-17s %s\n", editable ? "Mk" : "", "No.", "Name",
           g_kind == KIND_MAKER ? "Creator" : "Creator MAC",
           g_kind == KIND_HIST ? "Met" : "Created");

    for (int r = 0; r < LIST_ROWS; r++) {
        int i = g_scroll + r;
        if (i >= g_nrows) { printf("\n"); continue; }
        Row *row = &g_rows[i];
        printf("%s%s%s %4u %-10s %-17s %s%s\n",
               i == g_cursor ? C_GREEN ">" : " ",
               editable ? (row->mark ? "[x]" : "[ ]") : "   ",
               i == g_cursor ? C_RESET : "",
               (unsigned)row->idx, row->name, row->c2, row->date, C_RESET);
    }
    if (!d->loaded) printf(" File not loaded.\n");
    else if (!g_nrows) printf(" (no entries)\n");
    else printf(" " C_GRAY "%d/%d" C_RESET "\n", g_cursor + 1, g_nrows);
}

static void draw_list_bottom(void)
{
    consoleSelect(&g_bot);
    consoleClear();
    printf("\n " C_CYAN "Controls" C_RESET "\n");
    printf(" Up/Down  move     Left/Right  page\n");
    if (g_kind != KIND_MAKER) {
        printf(" A       mark / unmark entry\n");
        printf(" X       mark all / clear marks\n");
        printf(" Y       mark by name (keyboard)\n");
        printf(" START   delete marked entries\n");
        printf(" SELECT  apply to both files: %s\n", g_apply_both ? "ON" : "OFF");
    } else {
        printf(" This section is read-only.\n");
    }
    printf(" L/R     switch file\n B       back\n\n");
    if (g_msg[0])
        printf(" %s%s" C_RESET "\n", g_msg_level == 1 ? C_RED : g_msg_level == 2 ? C_GREEN : C_YELLOW, g_msg);
}

static void redraw(void)
{
    if (g_screen == SCR_MAIN) { draw_main(); draw_main_bottom(); }
    else if (g_screen == SCR_LIST) { draw_list(); draw_list_bottom(); }
}

/* ------------------------------------------------------------------ */
/* input                                                              */
/* ------------------------------------------------------------------ */
static void move_cursor(int *cur, int *scroll, int n, int delta)
{
    if (n <= 0) { *cur = *scroll = 0; return; }
    int c = *cur + delta;
    if (c < 0) c = 0;
    if (c >= n) c = n - 1;
    *cur = c;
    if (*cur < *scroll) *scroll = *cur;
    if (*cur >= *scroll + LIST_ROWS) *scroll = *cur - LIST_ROWS + 1;
    g_dirty = 1;
}

/* returns 1 when the program should quit */
static int handle_main(u32 k)
{
    if (k & KEY_DUP) { g_menu = (g_menu + MENU_N - 1) % MENU_N; g_dirty = 1; }
    if (k & KEY_DDOWN) { g_menu = (g_menu + 1) % MENU_N; g_dirty = 1; }
    int exitreq = (k & KEY_START) != 0;
    if (k & KEY_A) {
        switch (g_menu) {
        case 0: do_load(); break;
        case 1: enter_list(KIND_HIST); break;
        case 2: enter_list(KIND_INV); break;
        case 3: enter_list(KIND_MAKER); break;
        case 4: do_wipe(); break;
        case 5: do_save(); break;
        case 6: exitreq = 1; break;
        }
    }
    if (exitreq) {
        if (!any_changes() || confirm("There are unsaved changes.", "Exit and discard them?", NULL)) return 1;
    }
    return 0;
}

static void handle_list(u32 k)
{
    int editable = g_kind != KIND_MAKER;
    if (k & KEY_DUP) move_cursor(&g_cursor, &g_scroll, g_nrows, -1);
    if (k & KEY_DDOWN) move_cursor(&g_cursor, &g_scroll, g_nrows, 1);
    if (k & KEY_DLEFT) move_cursor(&g_cursor, &g_scroll, g_nrows, -LIST_ROWS);
    if (k & KEY_DRIGHT) move_cursor(&g_cursor, &g_scroll, g_nrows, LIST_ROWS);
    if (k & KEY_B) { g_screen = SCR_MAIN; g_dirty = 1; return; }
    if ((k & (KEY_L | KEY_R)) && g_db[1 - g_file].loaded) {
        g_file = 1 - g_file;
        g_cursor = g_scroll = 0;
        rebuild_rows();
    }
    if (!editable) return;
    if ((k & KEY_A) && g_nrows) { g_rows[g_cursor].mark ^= 1; g_dirty = 1; }
    if (k & KEY_X) {
        int all = count_marked() == g_nrows;
        for (int i = 0; i < g_nrows; i++) g_rows[i].mark = all ? 0 : 1;
        g_dirty = 1;
    }
    if (k & KEY_Y) mark_by_name();
    if (k & KEY_SELECT) { g_apply_both ^= 1; g_dirty = 1; }
    if (k & KEY_START) do_delete_marked();
}

/* ------------------------------------------------------------------ */
/* main                                                               */
/* ------------------------------------------------------------------ */
int main(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    gfxInitDefault();
    consoleInit(GFX_TOP, &g_top);
    consoleInit(GFX_BOTTOM, &g_bot);

    int mem_ok = 1;
    for (int i = 0; i < 2; i++) {
        g_db[i].work = malloc(DB_SIZE);
        g_db[i].orig = malloc(DB_SIZE);
        if (!g_db[i].work || !g_db[i].orig) mem_ok = 0;
    }
    g_tmpbuf = malloc(DB_SIZE + 1);
    if (!g_tmpbuf) mem_ok = 0;

    if (!mem_ok) {
        g_tried = 1;
        set_msg(1, "Out of memory.");
        g_db[0].err = g_db[1].err = ERR_NOMEM;
    } else {
        set_msg(0, "Select \"Load / reload databases\" (A) to begin.");
    }

    while (aptMainLoop()) {
        hidScanInput();
        u32 down = hidKeysDown();
        u32 held = hidKeysHeld();
        u32 k = down | key_repeat(down, held);

        int quit = 0;
        if (g_screen == SCR_MAIN) quit = handle_main(k);
        else handle_list(k);
        if (quit) break;

        if (g_dirty) {
            g_dirty = 0;
            redraw();
        }
        present();
    }

    arch_close();
    for (int i = 0; i < 2; i++) { free(g_db[i].work); free(g_db[i].orig); }
    free(g_tmpbuf);
    gfxExit();
    return 0;
}
