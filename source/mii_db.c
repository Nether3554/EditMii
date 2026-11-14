/* mii_db.c - see mii_db.h */
#include "mii_db.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------------ */
/* endian helpers                                                     */
/* ------------------------------------------------------------------ */
uint16_t rd16le(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
uint32_t rd32le(const uint8_t *p) { return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24); }
uint16_t rd16be(const uint8_t *p) { return (uint16_t)((p[0] << 8) | p[1]); }
uint32_t rd32be(const uint8_t *p) { return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3]; }
void wr16le(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
void wr32le(uint8_t *p, uint32_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24); }
void wr16be(uint8_t *p, uint16_t v) { p[0] = (uint8_t)(v >> 8); p[1] = (uint8_t)v; }

/* ------------------------------------------------------------------ */
/* CRC-16/XMODEM                                                      */
/* ------------------------------------------------------------------ */
static uint16_t g_crc_tab[256];
static int g_crc_ready = 0;

static void crc_init(void)
{
    for (int i = 0; i < 256; i++) {
        uint16_t c = (uint16_t)(i << 8);
        for (int k = 0; k < 8; k++)
            c = (c & 0x8000) ? (uint16_t)((c << 1) ^ 0x1021) : (uint16_t)(c << 1);
        g_crc_tab[i] = c;
    }
    g_crc_ready = 1;
}

uint16_t mii_crc16(const uint8_t *d, size_t n)
{
    if (!g_crc_ready) crc_init();
    uint16_t c = 0;
    for (size_t i = 0; i < n; i++)
        c = (uint16_t)((c << 8) ^ g_crc_tab[(c >> 8) ^ d[i]]);
    return c;
}

/* ------------------------------------------------------------------ */
/* basic accessors                                                    */
/* ------------------------------------------------------------------ */
int db_validate(const uint8_t *p, size_t size)
{
    if (size != DB_SIZE) return 1;
    if (memcmp(p, "CFOG", 4) || memcmp(p + CFHE_OFF, "CFHE", 4) || memcmp(p + CFRA_OFF, "CFRA", 4))
        return 2;
    return 0;
}

static int any_nonzero(const uint8_t *p, size_t n)
{
    for (size_t i = 0; i < n; i++)
        if (p[i]) return 1;
    return 0;
}

const uint8_t *db_maker_rec(const uint8_t *p, int k) { return p + MM_OFF + (size_t)k * MM_SZ; }

int db_maker_valid(const uint8_t *p, int k)
{
    const uint8_t *r = db_maker_rec(p, k);
    return r[0] == 3 && any_nonzero(r, MM_SZ);
}

int db_maker_count(const uint8_t *p)
{
    int n = 0;
    for (int k = 0; k < MM_N; k++)
        n += db_maker_valid(p, k);
    return n;
}

const uint8_t *db_node(const uint8_t *p, int i) { return p + NODE_OFF + (size_t)i * NODE_SZ; }
const uint8_t *db_hist_rec(const uint8_t *p, int i) { return p + HIST_OFF + (size_t)i * HIST_SZ; }
int db_node_used(const uint8_t *p, int i) { return any_nonzero(db_node(p, i), 10); }

int db_hist_used_list(const uint8_t *p, uint16_t *out)
{
    int n = 0;
    for (int i = 0; i < NODE_N; i++)
        if (db_node_used(p, i)) out[n++] = (uint16_t)i;
    return n;
}

int db_hist_order(const uint8_t *p, uint16_t *out)
{
    static uint8_t seen[NODE_N];
    uint16_t tail = rd16le(p + TAIL_OFF), head = rd16le(p + HEAD_OFF);
    if (head == IDX_EMPTY && tail == IDX_EMPTY) return 0;
    memset(seen, 0, sizeof seen);
    int n = 0;
    uint32_t cur = head;
    while (cur != IDX_NIL && cur != IDX_EMPTY) {
        if (cur >= NODE_N || seen[cur]) return -1;
        seen[cur] = 1;
        out[n++] = (uint16_t)cur;
        cur = rd16le(db_node(p, (int)cur) + 12) & 0x7FFF;
    }
    return n;
}

uint32_t db_inv_count(const uint8_t *p) { return rd32le(p + INV_COUNT_OFF); }
const uint8_t *db_inv_rec(const uint8_t *p, int i) { return p + INV_OFF + (size_t)i * INV_SZ; }

/* ------------------------------------------------------------------ */
/* consistency check                                                  */
/* ------------------------------------------------------------------ */
static uint16_t s_used[NODE_N], s_order[NODE_N], s_chain[NODE_N];
static int16_t  s_newidx[NODE_N], s_pos[NODE_N];

static void add_msg(char *msg, size_t len, int *bad, const char *text)
{
    size_t cur = strlen(msg);
    if (*bad && cur + 2 < len) { strcat(msg, "; "); cur += 2; }
    if (cur < len - 1) strncat(msg, text, len - 1 - cur);
    (*bad)++;
}

int db_check(const uint8_t *p, char *msg, size_t msglen)
{
    int bad = 0;
    char tmp[64];
    msg[0] = 0;

    if (rd16be(p + CRC1_OFF) != mii_crc16(p, CRC1_OFF))
        add_msg(msg, msglen, &bad, "CRC1 (Mii Maker + history) mismatch");
    if (rd16be(p + CRC2_OFF) != mii_crc16(p + CFRA_OFF, CRC2_OFF - CFRA_OFF))
        add_msg(msg, msglen, &bad, "CRC2 (invitations) mismatch");

    int nu = db_hist_used_list(p, s_used);
    int no = db_hist_order(p, s_order);
    int consistent = (no >= 0 && no == nu);
    if (consistent)
        for (int i = 0; i < no; i++)
            if (!db_node_used(p, s_order[i])) { consistent = 0; break; }

    if (!consistent) {
        add_msg(msg, msglen, &bad, "history list (CFHE) is inconsistent");
    } else {
        for (int i = 1; i < no; i++) {
            if ((rd16le(db_node(p, s_order[i]) + 10) & 0x7FFF) != s_order[i - 1]) {
                add_msg(msg, msglen, &bad, "history list: prev/next pointers do not match");
                break;
            }
        }
        if (no && rd16le(p + TAIL_OFF) != s_order[no - 1])
            add_msg(msg, msglen, &bad, "history list: incorrect tail index");
    }

    for (int k = 0; k < nu; k++) {
        int i = s_used[k];
        if (memcmp(db_hist_rec(p, i) + 0x0C, db_node(p, i), 10) != 0) {
            snprintf(tmp, sizeof tmp, "history record %d does not match its node", i);
            add_msg(msg, msglen, &bad, tmp);
            break;
        }
    }

    for (int i = 0; i < NODE_N; i++) {
        if (!db_node_used(p, i) && any_nonzero(db_hist_rec(p, i), HIST_SZ)) {
            snprintf(tmp, sizeof tmp, "non-empty history record in unused node %d", i);
            add_msg(msg, msglen, &bad, tmp);
            break;
        }
    }

    uint32_t n = db_inv_count(p);
    if (n > INV_N) {
        snprintf(tmp, sizeof tmp, "invitation count %lu > 100", (unsigned long)n);
        add_msg(msg, msglen, &bad, tmp);
    } else {
        uint8_t seen[INV_N];
        memset(seen, 0, sizeof seen);
        int ok = 1;
        for (uint32_t i = 0; i < n; i++) {
            uint8_t v = p[INV_ORDER_OFF + i];
            if (v >= n || seen[v]) { ok = 0; break; }
            seen[v] = 1;
        }
        if (!ok) add_msg(msg, msglen, &bad, "invitation order is not a permutation");
    }
    return bad;
}

void db_fix_crc(uint8_t *p)
{
    wr16be(p + CRC1_OFF, mii_crc16(p, CRC1_OFF));
    wr16be(p + CRC2_OFF, mii_crc16(p + CFRA_OFF, CRC2_OFF - CFRA_OFF));
}

/* ------------------------------------------------------------------ */
/* deletion                                                           */
/* ------------------------------------------------------------------ */
static uint8_t s_oldnodes[NODE_N * NODE_SZ];
static uint8_t s_oldrecs[NODE_N * HIST_SZ];

static void blank_history(uint8_t *p)
{
    for (int i = 0; i < NODE_N; i++) {
        uint8_t *n = p + NODE_OFF + (size_t)i * NODE_SZ;
        memset(n, 0, 10);
        wr16le(n + 10, IDX_NIL);
        wr16le(n + 12, IDX_NIL);
    }
    memset(p + HIST_OFF, 0, (size_t)NODE_N * HIST_SZ);
}

void db_clear_history(uint8_t *p)
{
    blank_history(p);
    wr16le(p + TAIL_OFF, IDX_EMPTY);
    wr16le(p + HEAD_OFF, IDX_EMPTY);
}

void db_clear_invitations(uint8_t *p)
{
    memset(p + INV_OFF, 0, (size_t)INV_N * INV_SZ);
    memset(p + INV_ORDER_OFF, 0, INV_N);
    wr32le(p + INV_COUNT_OFF, 0);
}

int db_delete_history(uint8_t *p, const uint8_t *drop)
{
    int nu = db_hist_used_list(p, s_used);
    int no = db_hist_order(p, s_order);
    if (no < 0 || no != nu) return -1;
    for (int i = 0; i < no; i++)
        if (!db_node_used(p, s_order[i])) return -1;

    for (int i = 0; i < NODE_N; i++) { s_newidx[i] = -1; s_pos[i] = -1; }
    int nk = 0;
    for (int i = 0; i < nu; i++)
        if (!drop[s_used[i]]) s_newidx[s_used[i]] = (int16_t)nk++;

    int nc = 0;
    for (int i = 0; i < no; i++)
        if (s_newidx[s_order[i]] >= 0) s_chain[nc++] = s_order[i];
    for (int k = 0; k < nc; k++) s_pos[s_chain[k]] = (int16_t)k;

    memcpy(s_oldnodes, p + NODE_OFF, sizeof s_oldnodes);
    memcpy(s_oldrecs, p + HIST_OFF, sizeof s_oldrecs);
    blank_history(p);

    for (int u = 0; u < nu; u++) {
        int old = s_used[u];
        int n = s_newidx[old];
        if (n < 0) continue;
        const uint8_t *on = s_oldnodes + (size_t)old * NODE_SZ;
        int k = s_pos[old];
        uint16_t prv = (k > 0) ? (uint16_t)s_newidx[s_chain[k - 1]] : IDX_NIL;
        uint16_t nxt = (k + 1 < nc) ? (uint16_t)s_newidx[s_chain[k + 1]] : IDX_NIL;
        uint16_t fprev = rd16le(on + 10), fnext = rd16le(on + 12);
        uint8_t *nn = p + NODE_OFF + (size_t)n * NODE_SZ;
        memcpy(nn, on, 10);
        wr16le(nn + 10, (uint16_t)(prv | (fprev & 0x8000)));
        wr16le(nn + 12, (uint16_t)(nxt | (fnext & 0x8000)));
        memcpy(p + HIST_OFF + (size_t)n * HIST_SZ, s_oldrecs + (size_t)old * HIST_SZ, HIST_SZ);
    }

    if (nc) {
        wr16le(p + TAIL_OFF, (uint16_t)s_newidx[s_chain[nc - 1]]);
        wr16le(p + HEAD_OFF, (uint16_t)s_newidx[s_chain[0]]);
    } else {
        wr16le(p + TAIL_OFF, IDX_EMPTY);
        wr16le(p + HEAD_OFF, IDX_EMPTY);
    }
    return nu - nk;
}

int db_delete_invitations(uint8_t *p, const uint8_t *drop)
{
    static uint8_t oldrecs[INV_N * INV_SZ];
    uint32_t n = db_inv_count(p);
    if (n > INV_N) return -1;

    uint8_t order[INV_N], neworder[INV_N];
    int newidx[INV_N];
    memcpy(oldrecs, p + INV_OFF, sizeof oldrecs);
    memcpy(order, p + INV_ORDER_OFF, n);

    int nk = 0;
    for (uint32_t i = 0; i < n; i++) newidx[i] = drop[i] ? -1 : nk++;

    memset(p + INV_OFF, 0, sizeof oldrecs);
    for (uint32_t i = 0; i < n; i++)
        if (newidx[i] >= 0)
            memcpy(p + INV_OFF + (size_t)newidx[i] * INV_SZ, oldrecs + (size_t)i * INV_SZ, INV_SZ);

    int no = 0;
    memset(neworder, 0, sizeof neworder);
    for (uint32_t j = 0; j < n; j++) {
        uint8_t o = order[j];
        if (o < n && newidx[o] >= 0) neworder[no++] = (uint8_t)newidx[o];
    }
    memset(p + INV_ORDER_OFF, 0, INV_N);
    memcpy(p + INV_ORDER_OFF, neworder, (size_t)no);
    wr32le(p + INV_COUNT_OFF, (uint32_t)nk);
    return (int)n - nk;
}

/* ------------------------------------------------------------------ */
/* text helpers                                                       */
/* ------------------------------------------------------------------ */
static char cp_to_ascii(uint32_t cp)
{
    static const char lat1_up[] = "AAAAAAACEEEEIIIIDNOOOOOxOUUUUYTs"; /* U+00C0..U+00DF */
    static const char lat1_lo[] = "aaaaaaaceeeeiiiidnooooo/ouuuuyty"; /* U+00E0..U+00FF */
    if (cp >= 0x20 && cp <= 0x7E) return (char)cp;
    if (cp >= 0xC0 && cp <= 0xDF) return lat1_up[cp - 0xC0];
    if (cp >= 0xE0 && cp <= 0xFF) return lat1_lo[cp - 0xE0];
    switch (cp) {
    case 0x104: return 'A'; case 0x105: return 'a';
    case 0x106: return 'C'; case 0x107: return 'c';
    case 0x118: return 'E'; case 0x119: return 'e';
    case 0x141: return 'L'; case 0x142: return 'l';
    case 0x143: return 'N'; case 0x144: return 'n';
    case 0x15A: return 'S'; case 0x15B: return 's';
    case 0x179: return 'Z'; case 0x17A: return 'z';
    case 0x17B: return 'Z'; case 0x17C: return 'z';
    default: return '?';
    }
}

void mii_name_ascii(const uint8_t *u, char *out)
{
    int n = 0;
    for (int i = 0; i < 10; i++) {
        uint32_t c = rd16le(u + i * 2);
        if (c == 0) break;
        if (c >= 0xD800 && c < 0xDC00 && i + 1 < 10) {   /* surrogate pair -> one '?' */
            uint32_t c2 = rd16le(u + (i + 1) * 2);
            if (c2 >= 0xDC00 && c2 < 0xE000) { i++; out[n++] = '?'; continue; }
        }
        out[n++] = cp_to_ascii(c);
    }
    out[n] = 0;
}

void fold_utf8_ascii(const char *in, char *out, size_t outsz)
{
    size_t n = 0;
    const uint8_t *s = (const uint8_t *)in;
    while (*s && n + 1 < outsz) {
        uint32_t cp;
        if (s[0] < 0x80) { cp = s[0]; s += 1; }
        else if ((s[0] & 0xE0) == 0xC0 && (s[1] & 0xC0) == 0x80) { cp = ((s[0] & 0x1Fu) << 6) | (s[1] & 0x3Fu); s += 2; }
        else if ((s[0] & 0xF0) == 0xE0 && (s[1] & 0xC0) == 0x80 && (s[2] & 0xC0) == 0x80) {
            cp = ((s[0] & 0x0Fu) << 12) | ((s[1] & 0x3Fu) << 6) | (s[2] & 0x3Fu); s += 3;
        } else if ((s[0] & 0xF8) == 0xF0 && (s[1] & 0xC0) == 0x80 && (s[2] & 0xC0) == 0x80 && (s[3] & 0xC0) == 0x80) {
            cp = 0x10000; s += 4;
        } else { cp = '?'; s += 1; }
        out[n++] = cp_to_ascii(cp);
    }
    out[n] = 0;
}

void fmt_mac(const uint8_t *m, char *out)
{
    snprintf(out, 18, "%02x:%02x:%02x:%02x:%02x:%02x", m[0], m[1], m[2], m[3], m[4], m[5]);
}

static void civil_from_days(int64_t z, int *y, unsigned *m, unsigned *d)
{
    z += 719468;
    int64_t era = (z >= 0 ? z : z - 146096) / 146097;
    unsigned doe = (unsigned)(z - era * 146097);
    unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    int64_t yy = (int64_t)yoe + era * 400;
    unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    unsigned mp = (5 * doy + 2) / 153;
    *d = doy - (153 * mp + 2) / 5 + 1;
    *m = mp < 10 ? mp + 3 : mp - 9;
    *y = (int)(yy + (*m <= 2));
}

/* seconds since 2000-01-01 (console clock) */
void fmt_date_2000(uint32_t sec, char *out, int with_time)
{
    int y; unsigned m, d;
    civil_from_days((int64_t)(sec / 86400u) + 10957, &y, &m, &d);
    unsigned rem = sec % 86400u;
    if (with_time) snprintf(out, 17, "%04d-%02u-%02u %02u:%02u", y, m, d, rem / 3600, (rem / 60) % 60);
    else           snprintf(out, 11, "%04d-%02u-%02u", y, m, d);
}

/* Mii ID (big endian at 0x0C): low 28 bits = 2-second ticks since 2010-01-01 */
void fmt_mii_created(const uint8_t *rec, char *out)
{
    uint32_t id = rd32be(rec + 0x0C);
    if (!id) { strcpy(out, "-"); return; }
    uint64_t secs = (uint64_t)(id & 0x0FFFFFFFu) * 2u;
    int y; unsigned m, d;
    civil_from_days((int64_t)(secs / 86400u) + 14610, &y, &m, &d);
    snprintf(out, 11, "%04d-%02u-%02u", y, m, d);
}
