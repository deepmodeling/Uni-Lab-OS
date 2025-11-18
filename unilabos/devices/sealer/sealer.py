import serial, time, re

class SimpleSealer:
    """
    It purposely skips CRC/ACK handling and sends raw commands of the form
    '**00??[=xxxx]zz!'.  Good enough for quick experiments or automation
    scripts where robustness is less critical.

    Example
    -------
    >>> sealer = SimpleSealer("COM24")
    >>> sealer.set_temperature(160)  # 160 °C
    >>> sealer.set_time(2.0)         # 2 s
    >>> sealer.seal_cycle()          # wait‑heat‑seal‑eject
    >>> sealer.close()
    """
    T_RE = re.compile(r"\*T\d\d:\d\d:\d\d=(\d+),(\d),(\d),")

    def __init__(self, port: str, baud: int = 19200, thresh_c: int = 150):
        self.port = port
        self.baud = baud
        self.thresh_c = thresh_c
        self.ser = serial.Serial(port, baud, timeout=0.3)
        self.ser.reset_input_buffer()

    # ---------- low‑level helpers ----------
    def _send(self, raw: str):
        """Write an already‑formed ASCII command, e.g. '**00DH=0160zz!'."""
        self.ser.write(raw.encode())
        print(">>>", raw)

    def _read_frame(self) -> str:
        """Read one frame (ending at '!') and strip the terminator."""
        return self.ser.read_until(b'!').decode(errors='ignore').strip()

    # ---------- high‑level commands ----------
    def set_temperature(self, celsius: int):
        self._send(f"**00DH={celsius:04d}zz!")

    def set_time(self, seconds: float):
        units = int(round(seconds * 10))
        self._send(f"**00DT={units:04d}zz!")

    def open_drawer(self):
        self._send("**00MOzz!")

    def close_drawer(self):
        self._send("**00MCzz!")

    def seal(self):
        self._send("**00GSzz!")

    # ---------- waits ----------
    def wait_temp(self):
        print(f"[Waiting ≥{self.thresh_c}°C]")
        while True:
            frame = self._read_frame()
            if frame.startswith("*T"):
                m = self.T_RE.match(frame)
                if not m:
                    continue
                temp = int(m.group(1)) / 10
                blk  = int(m.group(3))          # 1=Ready,4=Converging
                print(f"\rTemp={temp:5.1f}°C | Block={blk}", end="")
                if temp >= self.thresh_c and blk in (1, 0, 4):
                    print("  <-- OK")
                    return

    def wait_finish(self):
        while True:
            frame = self._read_frame()
            if frame.startswith("*T"):
                parts  = frame.split('=')[1].split(',')
                status = int(parts[1])
                cnt    = int(parts[6][: -3] if parts[6].endswith("!") else parts[6])
                print(f"\rRemaining {cnt/10:4.1f}s", end="")
                if status == 4:
                    print("\n[Seal Done]")
                    return

    # ---------- convenience helpers ----------
    def seal_cycle(self):
        """Full cycle: wait heat, seal, wait finish, eject drawer."""
        time.sleep(10)
        self.seal()
        self.open_drawer()

    def close(self):
        self.ser.close()


if __name__ == "__main__":
    # Quick demo usage (modify COM port and parameters as needed)
    sealer = SimpleSealer("COM24")
    try:
        sealer.set_temperature(160)   # °C
        sealer.set_time(2.0)          # seconds
        sealer.seal_cycle()
    finally:
        sealer.close()