# hil-run

通过 couart hub 对板子跑串口 HIL 用例，最后在标准输出打印 JSON 结论。退出码 0 表示通过，1 表示失败。

```sh
python3 ~/hil-run/hil_run.py --bench tests/hil/bench.yaml tests/hil/rtc
python3 ~/hil-run/hil_run.py --bench tests/hil/bench.yaml --check tests/hil/rtc   # 只查格式，不开串口
python3 ~/hil-run/hil_run.py --verify tests/hil/reports/<时间>                     # 回放报告，不开串口
python3 ~/hil-run/hil_run.py --self-test                                          # 跑 hil-run 自己的测试
```

依赖：`requirements.txt`（PyYAML）。开发和自测另装 `requirements-dev.txt`（pytest、pyflakes）。

## bench

```yaml
prompt: "boot>"
timeout_s: 8
devices:          # 用例里的逻辑设备名 -> couart hub 实例名
  ap: board_ap
clocks:           # 可选。名义频率，改频率只改这里
  rtc:
    hz: 32768
    tol_pct: 5
```

## 用例

顶层键：`name`、`steps`、`cleanup`、`timeout_s`、`prompt`。出现别的键会报错。

每一步只能是下面三种之一：

- `write: <命令>`：拿 TX 锁写一行，等提示符，判定这段回复。命令里可以用 `${name}` 引用前面保存的值。
- `read: true`：不发送，等这个设备上的新串口输出满足判定，用来抓中断这类异步打印。
- `sleep_ms: N`：主机等 N 毫秒，再判定这段时间里收到的输出。它只是等待，不当作测量的时间间隔。

步骤可用的键：`id`、`device`、`write`、`read`、`sleep_ms`、`expect`、`mutating`。几个设备同时执行时写成 `parallel: [步骤, ...]`，同一组里每个设备只能出现一次。`cleanup` 无论前面成败都会执行，已经卡住的设备会被跳过。

同一段步骤要对一组值各跑一遍时写成 `repeat`，跑之前摊开。几个名字按位置对齐（zip）。`${id}` 只替换 `repeat` 里的名字，前面 `save` 的 `${c0}` 不动：

```yaml
- repeat:
    id: [1, 2, 3]
  steps:
    - id: t${id}-c1
      device: ap
      write: chiptest_timer get_count ${id}
      expect:
        - pattern: '^timer ${id} count = (\d+)$'
          save: c${id}
```

### expect 条件

每一条必须有且只有一个选择器：`pattern`（正则，多行模式）、`absent`（不得匹配）、`equals`（去掉提示符行后整段相等）。`pattern` 后面可以加下面这些比较器，全部满足才算通过：

| 键 | 含义 |
| --- | --- |
| `count` | 匹配次数必须等于这个数 |
| `capture_set` | 第 1 组捕获恰好是这个集合，每个值出现一次（`"0..15"` 或列表） |
| `range` | 第 1 组是整数，落在 `{min, max}` 内；也可以写 `{clock}` 或 `{hz, tol_pct}` |
| `gt` `ge` `eq` `lt` `le` | 第 1 组和一个整数比较，或和前面保存的名字比较（只能写一个） |
| `save` | 把第 1 组按整数保存，供后面的步骤使用 |
| `save_ms` | 保存这次匹配到达主机的时刻，单位毫秒 |
| `elapsed_ms` | 这次匹配的到达时刻减去 `since`；写 `{since, min, max}` 或 `{since, ms, tol_pct}` |
| `rate_hz` | `(本次捕获 - tick) * 1000 / (本次到达 - time)`；写 `{tick, time, min, max}`、`{tick, time, hz, tol_pct}` 或 `{tick, time, clock}` |

到达时刻来自 couart `HISTORYT`，即每批字节进入 hub 时的 CLOCK_MONOTONIC。hub 不支持 `HISTORYT` 时，带时间的条件会以 `no byte timestamp` 失败，不会自己编一个时间。USB 串口打包有 10ms 到 16ms 的误差，测频率时两次读数的间隔建议 400ms 以上。

未知的键一律报错，避免拼错的条件被悄悄忽略。

## 报告

不传 `--report-dir` 时，报告写到 `<hil-run 上一级>/tests/hil/reports/<时间>/`：

- `summary.txt`：每步一行结论。
- `report.json`：`format` 是报告格式版本。每一步记录展开后的条件、匹配结果、窗口文件的 sha256，以及每个字符的到达时刻 `times_ms`。
- `windows/<用例>/<步骤>.txt`：判定时看到的原始串口文本。

`--verify` 读 `report.json` 和窗口文件，重新判定每一步，确认和当时的结论一致。没有 `format` 的旧报告按第 1 版处理。报告格式有不兼容的改动时，必须增大 `hilrun/report.py` 里的 `REPORT_FORMAT`。

## 代码结构

| 模块 | 内容 |
| --- | --- |
| `hil_run.py` | 入口，保持原来的调用路径 |
| `hilrun/couart.py` | couart 控制套接字：TX 锁、写一行、`HISTORY` / `HISTORYT`、窗口切片 |
| `hilrun/compare.py` | expect 选择器和比较器 |
| `hilrun/plan.py` | 读取 bench 和用例、格式检查、`clocks` 展开 |
| `hilrun/runner.py` | 执行步骤、并行组、cleanup、卡住的设备 |
| `hilrun/report.py` | 步骤记录、报告文件、`--verify` |
| `hilrun/cli.py` | 命令行 |
| `tests/` | pytest；`fakehub.py` 是假的 couart hub，不接板子也能跑端到端测试 |

### 加一个比较器

在 `hilrun/compare.py` 里写一个 `Comparator` 子类：

- `keys`：这个比较器占用的 YAML 键。
- `validate`：检查格式。
- `check`：判定，返回 `None` 或失败原因。
- `report` / `describe`：写进报告的字段和摘要。
- `commit`：写入保存值，只在整条都通过后执行。
- 需要到达时刻时设 `timed = True`。

然后按判定顺序放进 `COMPARATORS`，在 `tests/test_compare.py` 里补测试。格式检查、判定、报告和回放会自动用上它。
