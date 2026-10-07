# SD config experiment variants

Each directory here is **one experiment** on the MPS3 config SD: a named set of
`KEY: VALUE` overrides applied on top of `../templates/` by

```bash
../assemble_sd.sh C <shell.bit> --variant <NAME>
```

They exist because the MCC boot lottery (a config from the SD that sometimes
does not complete — [`docs/BOOT_RATE.md`](../../../docs/BOOT_RATE.md)) can only
be attacked by changing **one key at a time and counting**, and a hand-edited SD
cannot be named in a run record, compared, or repeated three weeks later.

## Layout

```
variants/<NAME>/
├── variant.txt     ; the rationale, in comments; DESCRIPTION: one line
├── config.txt      ; overrides for templates/config.txt      (optional)
├── nanosoc.txt     ; overrides for templates/nanosoc.txt     (optional)
└── board.txt       ; overrides for templates/board.txt       (optional)
```

Override files carry only the keys that change, in the same `KEY: VALUE ;comment`
dialect as the templates. `DESCRIPTION:` in `variant.txt` is required — a
variant nobody can describe is one nobody can interpret in a result.

## Rules the flow enforces

* **A key must already exist in the template.** `assemble_sd.sh` refuses one
  that does not, loudly. An MPS3 config file accepts almost anything silently
  and fails at the I/O pads with no message, so an added key would ship a card
  that claims an experiment it is not running.
* **No variant selected changes nothing.** The default bundle is byte-identical
  to the templates — proven in `host/pyverify/tests/test_bootrate.py`, along
  with "a variant changes exactly the keys it declares".
* **Each patched file is stamped** with a `;VARIANT: <NAME>` banner, so a card
  found in a board says which experiment it carries.
* **The harness reads these directories**, it is not told about them:
  `pyverify boot-rate --variant <NAME>` copies the key deltas from here into the
  run record, and refuses a name that has no directory.

## Adding one

1. `mkdir variants/<NAME>` with a `variant.txt` whose comments say **why this
   key is a suspect**, citing the template line or the TRM section that makes it
   one. A suspect nobody can cite is a guess.
2. Add the override file(s), one variable's worth of keys and no more.
3. `../assemble_sd.sh C --variant <NAME>` and read the diff against a default
   bundle.
4. `cd host/pyverify && python3 -m pytest tests/test_bootrate.py -q` — every
   shipped variant is applied and read back by a test, so a broken one is caught
   here rather than on the board.
