# CubeSat Ground Station: package notes

Python 3.10, standard library only (plus pytest for the tests). Everything runs offline:
the network layer talks to TCP servers that you choose, and a local mock server is included
for demos and tests.

## What is in the window

`python -m groundstation` (or `run_app.bat` in the repository root) opens one window with four tabs, all of them live:

| Tab | State |
|---|---|
| Control | Live. Stage values for configuration parameters 0 to 39 and send only the staged ones (COP-1 control request). Shows the acknowledgement, a rejection or "no response" per parameter. Selecting a row shows that row's staged value in the Value box (or an empty box), never the text of the previous row. |
| Monitoring | Live. Sends a COP-1 monitoring request, optionally on a timer, and shows the most recent report: configuration table, status table, error flags, COP-1 version and TM port. |
| Telecommand | Live. Build one request at a time (BD segment, AD segment, CLTU, Transfer Frame, directive, NOP, output flow subscription), send it, and follow the replies in a requests table (pending, accepted, rejected, confirmed, echo received, rejected (-1), no response). "Validate limits" can be switched off to send out-of-range data on purpose. Hex data is typed in groups separated by spaces, commas or colons; every group must have an even number of digits (so `AB CD` and `0xABCD` pass, `A BCD` does not). Each reply writes exactly one log entry (the short line and the field dump are one entry). |
| Telemetry | Live. The table built from a JSON parameter file (Load config..., Start, Stop) is fed either by the built-in simulated source (default, starts at launch) or, in Network mode, by CORTEX telemetry received over TCP. Request fields: channel, buffered blocks, data flow, frame mask, expected value. A status block shows the stream state, message and gap counters, the frame check results and the last header values (time tags are shown as raw words, not decoded). Frames that still arrive after Stop was sent never reach the table; they are counted as "ignored after stop" and the first one is logged. A count that keeps growing means the server is not honouring Stop. |

All four tabs have a connection bar (host, port, Connect/Disconnect), a
"Protocol options..." dialog shared by all tabs, a log with an optional raw-word dump, and a
statistics line. The default window size is 1000x720 and the minimum is 900x600.
The Telemetry table's requested height follows the display scaling (Tk's scaling relative to 96 dpi), because
the log is sized in text lines and the two must scale together. At 125 percent or more on the default
window size the log is squeezed (at 150 percent it can disappear, with its "Show raw words" and "Clear"
controls); maximise the window to get the room back.

### A silent server is visible: the reply timeout

A server that accepts the connection and never answers must not look like success. The reply
timeout is 5 seconds by default and is set in "Protocol options..." as "Reply timeout (s)"
(a number from 0.5 to 300, ASCII digits only; an invalid value is refused and the old one is kept).
It applies to requests sent after the change. The check runs in the existing 50 ms pump (no extra thread).

| Tab | What a missing reply looks like | Cleared by |
|---|---|---|
| Control | the parameter rows read "no response (timeout)" | the late acknowledgement, which replaces the text |
| Telecommand | the row reads "no response (timeout)". Every operation currently expects a reply (the Accept/Reject reply carries the operation code); this is our reading, not confirmed. Only rows still "pending" time out, not rows that were accepted and wait for a confirm. | a late reply |
| Monitoring | the stale label reads "stale (no report within N s)", also before the first report. Auto refresh keeps running. | the next report |
| Telemetry | the status block reads "no frame yet, N s since the request" while the stream state is "requested". Informational: no error, counters untouched. There is no silence alarm while streaming, because the real frame rate is unknown. | the first frame, Stop, a refusal or a lost link |

A timeout is a display state, never a discard: a reply that arrives later is processed normally and
replaces the timeout text. Every timeout writes one log line with the usual hint (flow ID, message
size, checksum mode). If the link is lost, rows still waiting read "no response (disconnected)" as before.

### Crash log

`python -m groundstation` (and `python -m groundstation.app`) install handlers for Tk callbacks, the main
thread and worker threads. Every unhandled exception is printed with its traceback and appended to
`groundstation_errors.log` in the current directory (if that file cannot be written, the traceback still
goes to the console). A missing tkinter, a Python older than 3.10 or a window that cannot open
(no display, broken Tcl) give a plain message and exit code 2 instead of a traceback.

## How to run

From the repository root (the folder that contains `groundstation/`):

```
python -m groundstation --config samples/sample_config_5.json
```

`python -m groundstation.app` still works and does the same. On Windows `run_app.bat`, `run_mock.bat`
and `run_tests.bat` in the repository root find Python 3.10 or newer (`python`, then `py -3`) and pass
their arguments on.

Options: `--config PATH` (parameter file for the Telemetry tab; relative paths are also tried
inside `groundstation/`), `--rate HZ` and `--corrupt P` (simulated telemetry source),
`--host HOST` (default 127.0.0.1) and `--port-offset N` (added to the default Control port 3101,
Monitoring port 3100, Telecommand port 3120 and Telemetry port 3070).

## Offline demo with the mock servers

In one terminal:

```
python -m groundstation.mockserver_cli
```

It prints `READY <service> <host>:<port>` for the control (3101), monitoring (3100),
telecommand (3120) and telemetry (3070) services, then `ALL READY`, and runs until Ctrl+C.
Options: `--host`, `--port-offset`, `--config` (parameter file used for the mock telemetry
frames) and `--rate`. The four services share one parameter store, so values sent with the
Control tab appear in the Monitoring report.

Before connecting to anything, `python -m groundstation.selfcheck` checks the installation against built-in mock
servers, and `python -m groundstation.selfcheck --host H` sends one read-only monitoring request to a real
server (see the repository `README.md`). In another terminal run the app (use the same `--port-offset` if you set one), then press
Connect on the tab you want to try. For live telemetry open the Telemetry tab, press Connect, choose
Source "Network", then press the table's Start button (Stop ends the stream).
The mock's telemetry frames follow `samples/sample_config_10.json` (the `--config` of `mockserver_cli`);
start the app with `--config samples/sample_config_10.json` to see all ten values decoded.

**The mock server is a test double.** It was written from our reading of the COP-1 and CORTEX
interface documents. It is
not the real COP-1 or CORTEX software, so a successful demo shows that our reading is
self-consistent, not that it matches the real servers.

## Tests

```
python -m pytest
```

The GUI tests use one shared hidden Tk window and need a display. Network tests use
127.0.0.1 with OS-chosen ports. The reply timeout and stall tests use fake or virtual clocks, so a loaded machine
does not change their result; run the whole suite with nothing else heavy anyway.

## Source layout

- `protocol.py`, `cop1.py`, `cortex.py`: message framing, COP-1 and CORTEX builders and parsers.
- `net.py`, `clients.py`: TCP client with a reader thread and an event queue, one client per service.
- `mockserver.py`, `mockserver_cli.py`: the mock services and their command line.
- `guibase.py`, `tab_control.py`, `tab_monitoring.py`, `tab_telecommand.py`, `tab_telemetry.py`, `app.py`: the GUI.
- `__main__.py`: `python -m groundstation` with the Python and tkinter checks. `crashlog.py`: the crash handlers. `selfcheck.py`: the diagnostic tool.
- `config.py`, `convert.py`, `decode.py`, `source.py`, `tm.py`, `gui.py`: the config-driven telemetry table.
- `netsource.py`: the network frame source and the switch between simulated and network frames.
- `samples/`: test parameter files, not real mission data.

## Parameter file conventions

Set as constants at the top of `config.py`: a parameter's `position` is read as a zero-based
byte offset, and the default endianness is big. These are chosen defaults.

## Assumptions

These are unconfirmed against the real server and can be changed in "Protocol options...":

1. Checksum coverage: which words the checksum covers (whole packet, everything except the
   postamble, or payload only).
2. Byte numbering (`cortex_byte0_is_msb`): whether byte 0 of the CORTEX frame check word is the
   most significant byte.
3. Per-service flow IDs: the flow ID used in each service's message header.
4. FEC in frame: whether the forward error correction bytes are inside the frame (this changes
   the maximum segment length).
