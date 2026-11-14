/*
 * mii_db.h - portable core for the 3DS Mii database (CFL_DB.dat / CFL_OldDB.dat)
 *
 * Works on the DECODED data (0x4BD20 bytes) exactly as returned by the 3DS
 * file system (FS) for the files inside shared extdata 0xF000000B.
 * No 3DS-specific code here, so it can be tested on a PC.
 */
#ifndef MII_DB_H
#define MII_DB_H

#include <stdint.h>
#include <stddef.h>

#define DB_SIZE        0x4BD20u

/* Mii Maker section (never modified by this program) */
#define MM_OFF         0x08
#define MM_N           100
#define MM_SZ          0x5C
#define MAKER_END      0x23F8

/* History (encountered Miis) */
#define CFHE_OFF       0x23F8
#define TAIL_OFF       0x23FC
#define HEAD_OFF       0x23FE
#define NODE_OFF       0x2400
#define NODE_N         3000
#define NODE_SZ        14
#define HIST_OFF       0xE4C0
#define HIST_SZ        0x54

/* Invitations (Mii Plaza) */
#define CRC1_OFF       0xC81E
#define CFRA_OFF       0xC820
#define INV_COUNT_OFF  0xC824
#define INV_ORDER_OFF  0xC828
#define INV_OFF        0xC88C
#define INV_N          100
#define INV_SZ         0x48
#define CRC2_OFF       0xE4BE

#define IDX_NIL        0x7FFF
#define IDX_EMPTY      0xFFFF

/* little/big endian helpers (alignment safe) */
uint16_t rd16le(const uint8_t *p);
uint32_t rd32le(const uint8_t *p);
uint16_t rd16be(const uint8_t *p);
uint32_t rd32be(const uint8_t *p);
void     wr16le(uint8_t *p, uint16_t v);
void     wr32le(uint8_t *p, uint32_t v);
void     wr16be(uint8_t *p, uint16_t v);

uint16_t mii_crc16(const uint8_t *d, size_t n);

/* 0 = looks like a Mii database, 1 = wrong size, 2 = wrong signatures */
int  db_validate(const uint8_t *p, size_t size);

/* Mii Maker (read only) */
int  db_maker_valid(const uint8_t *p, int k);
int  db_maker_count(const uint8_t *p);
const uint8_t *db_maker_rec(const uint8_t *p, int k);

/* history */
const uint8_t *db_node(const uint8_t *p, int i);       /* 14 bytes: ID(4) MAC(6) prev(2) next(2) */
const uint8_t *db_hist_rec(const uint8_t *p, int i);   /* 0x54 bytes */
int  db_node_used(const uint8_t *p, int i);
int  db_hist_used_list(const uint8_t *p, uint16_t *out);  /* returns count, out[NODE_N] */
int  db_hist_order(const uint8_t *p, uint16_t *out);      /* count, or -1 if list is corrupted */

/* invitations */
uint32_t db_inv_count(const uint8_t *p);
const uint8_t *db_inv_rec(const uint8_t *p, int i);    /* 0x48 bytes */

/* consistency; returns number of problems, text goes to msg (joined with "; ") */
int  db_check(const uint8_t *p, char *msg, size_t msglen);
void db_fix_crc(uint8_t *p);

/* deletion. drop[] = array of flags indexed by entry number.
 * return: number of removed entries, -1 = structure corrupted */
int  db_delete_history(uint8_t *p, const uint8_t *drop /* [NODE_N] */);
int  db_delete_invitations(uint8_t *p, const uint8_t *drop /* [INV_N] */);
void db_clear_history(uint8_t *p);
void db_clear_invitations(uint8_t *p);

/* text helpers (ASCII only output - the default 3DS console font is ASCII) */
void mii_name_ascii(const uint8_t *utf16le20, char *out /* >= 11 bytes */);
void fold_utf8_ascii(const char *in, char *out, size_t outsz);
void fmt_mac(const uint8_t *mac6, char *out /* >= 18 */);
void fmt_date_2000(uint32_t sec, char *out /* >= 17 */, int with_time);
void fmt_mii_created(const uint8_t *rec, char *out /* >= 11 */);

#endif
