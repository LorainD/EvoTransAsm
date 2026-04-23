## RVV迁移包含三层：
- 实现层：.S中的rvv函数
- 绑定层：*_init.c中的extern声明与函数指针绑定
- 调度层：源.c或.h中(其他架构声明所在位置)#if ARCH_RISCV接入

**仅当创建新的.S和.c文件时，才允许修改Makefile和调度层。**
**至少会修改实现层的.S文件和绑定层的.c文件**

## RVV.S 开头声明
```
#include "libavutil/riscv/asm.S"
```

.S函数格式示例：
```

func ff_ac3_exponent_min_rvv, zve32x
        lpad    0
        beqz     a1, 3f
1:
        vsetvli  t2, a2, e8, m8, ta, ma
        vle8.v   v8, (a0)
        addi     t0, a0, 256
        sub      a2, a2, t2
        mv       t1, a1
2:
        vle8.v   v16, (t0)
        addi     t1, t1, -1
        vminu.vv v8, v8, v16
        addi     t0, t0, 256
        bnez     t1, 2b

        vse8.v   v8, (a0)
        add      a0, a0, t2
        bnez     a2, 1b
3:
        ret
endfunc
```
