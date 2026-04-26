# 迁移通用模板
## RVV迁移包含三层：
- 实现层：.S中的rvv函数
- 绑定层：*_init.c中的rvv函数声明与函数指针绑定
- 调度层：仿照源文件下的其他架构声明（使用view_file工具查看）：#elif ARCH_RISCV接入只能写在 #if ARCH_其他架构 下，#endif 之前
        - 可能性1：源.c或#elif ARCH_RISCV接入，.h中init函数声明
        - 可能性2：.h 直接 #elif ARCH_RISCV接入
        示例
        ```
        #if ARCH_AARCH64
                ff_color_detect_dsp_init_aarch64(dsp, depth, color_range);
        #elif ARCH_X86 && HAVE_X86ASM
                ff_color_detect_dsp_init_x86(dsp, depth, color_range);
        #elif ARCH_RISCV
                ff_color_detect_dsp_init_riscv(dsp, depth, color_range); 
        #endif
        ```
        声明的ff_xx_init_riscv()函数名与参数需要参考其他架构来定义，并在绑定层中实现

**仅当创建新的.S和.c文件时，才允许修改Makefile和调度层。**
**至少会修改实现层的.S文件和绑定层的.c文件**

## RVV.S 格式示例

- .S函数格式示例：
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
- 如果新建.S文件则开头声明示例：
```
#include "libavutil/riscv/asm.S"
```


# 个性化经验
- sw_开头的模块: c源代码位于libswscale库下，源代码所在位置可以参考x86架构的{symbol}.c函数中的#include 文件
- 强约束：如果没有新建文件，一定只修改.S和用于声明rvv的.c文件（都是existing-rvv）
- 强约束：如果新建文件，一定需要添加绑定，参考其他架构的经验链接到c源实现的部分，进行架构分发函数的链接

# 报错修复指导

1. 如果你遇到类似错误：“ no previous prototype for 'ff_xxx_init_riscv'”或是 init.c链接时报错"No such file or directory"
该错误说明没有在源c文件下添加该init入口，需要添加调度层绑定，可以通过查看其他架构下的.c文件（如libxx/x86/xx_init.c）的#include 文件 找到源架构层位置,并仿照其完整链接，填充riscv的声明

2. 如果checkasm成功构建但测试失败“0 tests executed”
说明调度层绑定错误，先检查makefile链接，再通过查看其他架构的init.c文件的#include 文件找到该架构的声明位置，并仿照填充riscv的声明