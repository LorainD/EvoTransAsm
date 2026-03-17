# rvv-agent run report

## Symbol

- ff_vp8_idct16_add

## Plan

- 意图解析：迁移 ff_vp8_idct16_add
- 定位 C 实现
- 定位 x86 / ARM 参考实现
- 语义抽象（结构化任务描述 JSON）
- （MVP）调用 LLM 生成 RVV asm + init + Makefile patch（先落到 runs/）
- （可选）把补丁应用到 workspace
- （可选）交叉 configure + build checkasm
- 生成 run 报告（轨迹、输入输出、命令、摘要）

## Discovery

### c_candidates

- (none)

### x86_refs

- (none)

### arm_refs

- (none)

### aarch64_refs

- (none)

### riscv_refs

- (none)

### headers

- (none)

### other

- (none)

## Matches (first 200)


## Analysis JSON

```json
{
  "symbol": "ff_vp8_idct16_add",
  "datatype": "unknown",
  "vectorizable": true,
  "pattern": [],
  "has_stride": false,
  "has_saturation": false,
  "reduction": false,
  "tail_required": false,
  "math_expression": "unknown",
  "c_candidates": [],
  "x86_refs": [],
  "arm_refs": [],
  "notes": "LLM 未运行或解析失败，使用 fallback。"
}
```

- llm_used: False

- error: Missing API key: env LLM_API_KEY is empty

## Generation (raw)

```
Missing API key: env LLM_API_KEY is empty
```

## Materialized

- runs/20260303_072922_ff_vp8_idct16_add/artifacts/files/libavcodec/riscv/ff_vp8_idct16_add_rvv.S

## configure

- (skipped)

## make checkasm

- (skipped)
