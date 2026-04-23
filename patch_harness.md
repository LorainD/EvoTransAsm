## RVV迁移包含三层：
- 实现层：.S中的rvv函数
- 绑定层：*_init.c中的rvv函数声明与函数指针绑定
- 调度层：仿照源文件下的其他架构声明（使用view_file工具查看）：源.c(其他架构声明所在位置)#if ARCH_RISCV接入，.h中init函数声明

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

## riscv/xxx_init.c示例

```
#include "config.h"
#include "libavutil/attributes.h"
#include "libavutil/cpu.h"
#include "libavcodec/xxx.h" // 引入c源代码所在头文件

// [核心规则 1] 汇编函数本地声明区：
// 所有带有 _rvv / _rvi 后缀的汇编函数，必须且只能在此处声明！绝对禁止写入通用 .h 文件！
void ff_[func_name]_rvv(...);

// [核心规则 2] 架构入口定义：与通用 .h文件下一致
av_cold void ff_[module_name]_init_riscv([Module]Context *s)
{
// [核心规则 3] 编译时宏隔离
#if HAVE_RVV 
    int flags = av_get_cpu_flags();

    // [核心规则 4] 运行时标志位检查 (按汇编需求使用 RVV_I32, RVV_F32, RVB 等)
    if (flags & AV_CPU_FLAG_RVV_F32) { 
        // [核心规则 5] 函数指针绑定
        s->[target_func_ptr] = ff_[func_name]_rvv; 
    }
#endif
}
```

## 个性化经验
- sw_开头的模块: c源代码位于libswscale库下，所在位置可以参考x86架构的.c函数中的#include 文件

