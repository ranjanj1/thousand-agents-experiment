# Build

From the workspace root (uses the vendored crates configured in `.cargo/config.toml`, no network):

```sh
cargo build --release --offline
mkdir -p bin && cp target/release/tinycoder target/release/tinycoder-agent bin/
```

`bin/tinycoder` is the release binary itself: it serves `serve files|shell` and execs
`bin/tinycoder-agent` (which must sit next to it) for `run` and `batch`. The agent reads
`shared/system.txt` at run time by walking up from the binary's location (so keep `bin/` and
`shared/` side by side).

Optional: `cargo test --release --offline` runs the unit tests.
