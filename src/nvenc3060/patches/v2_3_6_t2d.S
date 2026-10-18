/* Experimental, CPU-verifiable replacement for the two inline NV12 packers.
 * Assemble each variant with clang -target x86_64-unknown-linux-gnu -c.
 * Addresses are for the exact v2.3.6 H.264 dylib in profiles/v2.3.6.json.
 * This is a layout correction candidate, not GPU qualification.
 *
 * The existing caller stages a complete macroblock-aligned row in r14.
 * r12d = row, pitch is 64-byte aligned. Both planes have 16-byte width units.
 * The byte position is classic t2d with block height two GOBs:
 *   (y/16)*pitch*16 + (x/64)*1024 + (y&8)*64 +
 *   (x&32)*8 + (y&4)*32 + (x&16)*4 + (y&3)*16 + (x&15).
 * Moving 16 contiguous bytes preserves every low four x bits.
 * No register live at the original outer-loop continuation is changed.
 */
.text
.globl patch_start
patch_start:
#ifdef CHROMA
.set BASE, 0x64d9
.set CONTINUE, 0x6450
#else
.set BASE, 0x63aa
.set CONTINUE, 0x6330
#endif
    movl %r12d, %eax
    andl $-16, %eax
#ifdef CHROMA
    imull 0x10(%r9), %eax
#else
    imull 0x10(%r15), %eax
#endif
    movl %r12d, %ecx
    andl $8, %ecx
    shll $6, %ecx
    movl %r12d, %edi
    andl $4, %edi
    shll $5, %edi
    orl %edi, %ecx
    movl %r12d, %edi
    andl $3, %edi
    shll $4, %edi
    orl %edi, %ecx
    orl %eax, %ecx
    xorl %esi, %esi
    xorl %edx, %edx
1:
    movl %ecx, %edi
    addl %edx, %edi
    movups (%r14,%rsi), %xmm0
#ifdef CHROMA
    movups %xmm0, (%r10,%rdi)
#else
    movups %xmm0, (%rbx,%rdi)
#endif
    addq $16, %rsi
    /* Increment the sparse x address, skipping unused bits 7 and 9. */
    orl $0x280, %edx
    addl $64, %edx
    andl $-641, %edx
#ifdef CHROMA
    cmpq %rbx, %rsi
#else
    cmpq %r9, %rsi
#endif
    jl 1b
    .byte 0xe9
    .long CONTINUE - (BASE + . - patch_start + 4)
.globl patch_end
patch_end:
