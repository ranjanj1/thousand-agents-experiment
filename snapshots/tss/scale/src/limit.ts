// Admission control for the child processes of `run_tests` / `run_command` in `batch`.
//
// Starting a test run is mostly CPU (a Python start-up alone is ~40-160 ms of CPU here), so when the machine is
// busy, running more at once than there are cores only adds memory, not throughput. But a command may also sit idle
// (sleep, wait on I/O) and must not hold up everyone else. So:
// - a process counts against `soft` only during its first `youngMs`; after that only against `hard`;
// - and while the CPUs are measurably idle (sampled every TICK_MS), up to `soft` more processes are let in per tick
//   beyond `soft`, still within `hard`.
// Slots are granted in request (FIFO) order.
import * as os from "node:os";

const TICK_MS = 50;
const IDLE_ENOUGH = 0.25; // fraction of all CPU time spent idle in the last tick

function cpuTimes(): { idle: number; all: number } {
  let idle = 0;
  let all = 0;
  for (const c of os.cpus()) {
    const t = c.times;
    idle += t.idle;
    all += t.user + t.nice + t.sys + t.idle + t.irq;
  }
  return { idle, all };
}

export class Limiter {
  private young = 0;
  private total = 0;
  private readonly waiting: (() => void)[] = [];
  private ticker: NodeJS.Timeout | undefined;
  private last = cpuTimes();

  constructor(
    readonly soft: number,
    readonly hard: number,
    readonly youngMs: number,
  ) {}

  /** Resolves when a slot is free. Call the returned function exactly once to give it back. */
  acquire(): Promise<() => void> {
    return new Promise((resolve) => {
      this.waiting.push(() => {
        this.young++;
        this.total++;
        let isYoung = true;
        let released = false;
        const age = setTimeout(() => {
          isYoung = false;
          this.young--;
          this.pump();
        }, this.youngMs);
        age.unref();
        resolve(() => {
          if (released) return;
          released = true;
          clearTimeout(age);
          if (isYoung) this.young--;
          this.total--;
          this.pump();
        });
      });
      this.pump();
    });
  }

  private grant(): void {
    this.waiting.shift()!();
  }

  private pump(): void {
    while (this.waiting.length && this.young < this.soft && this.total < this.hard) this.grant();
    if (this.waiting.length && !this.ticker) {
      this.last = cpuTimes();
      this.ticker = setInterval(() => this.tick(), TICK_MS);
    } else if (!this.waiting.length && this.ticker) {
      clearInterval(this.ticker);
      this.ticker = undefined;
    }
  }

  private tick(): void {
    const now = cpuTimes();
    const all = now.all - this.last.all;
    const idle = all > 0 ? (now.idle - this.last.idle) / all : 0;
    this.last = now;
    if (idle >= IDLE_ENOUGH) {
      for (let n = 0; n < this.soft && this.waiting.length && this.total < this.hard; n++) this.grant();
    }
    this.pump();
  }
}
