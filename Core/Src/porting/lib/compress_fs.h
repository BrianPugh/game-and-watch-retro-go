#ifndef COMPRESS_FS_H
#define COMPRESS_FS_H

/***
 * Goals: simple single-file-at-a-time read/write with compression.
 * Main intent is to be used for game savedata.
 *
 * These functions can be easily reworked once an actual fs is implemented.
 */

int fopen_compress(uint8_t *storage);

int fwrite_compress(uint8_t *src, size_t count);

int fread_compress(uint8_t *dst, size_t count);

int fclose_compress();


#endif
