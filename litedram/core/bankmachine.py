from migen import *
from migen.genlib.misc import WaitTimer

from litex.soc.interconnect import stream

from litedram.core.multiplexer import *


class _AddressSlicer:
    def __init__(self, colbits, address_align):
        self.colbits = colbits
        self.address_align = address_align

    def row(self, address):
        split = self.colbits - self.address_align
        if isinstance(address, int):
            return address >> split
        else:
            return address[split:]

    def col(self, address):
        split = self.colbits - self.address_align
        if isinstance(address, int):
            return (address & (2**split - 1)) << self.address_align
        else:
            return Cat(Replicate(0, self.address_align), address[:split])


class BankMachine(Module):
    def __init__(self, n, aw, address_align, settings):
        self.req = req = Record(cmd_layout(aw))
        self.refresh_req = Signal()
        self.refresh_gnt = Signal()
        a = settings.geom.addressbits
        ba = settings.geom.bankbits
        self.cmd = cmd = stream.Endpoint(cmd_request_rw_layout(a, ba))

        # # #

        # Command buffer
        cmd_buffer_layout = [("we", 1), ("adr", len(req.adr))]
        if settings.cmd_buffer_depth < 2:
            cmd_buffer = stream.Buffer(cmd_buffer_layout)
        else:
            cmd_buffer = stream.SyncFIFO(cmd_buffer_layout,
                                         settings.cmd_buffer_depth)
        self.submodules += cmd_buffer
        self.comb += [
            req.connect(cmd_buffer.sink, omit=["wdata_valid", "wdata_ready",
                                               "rdata_valid", "rdata_ready",
                                               "lock"]),
            cmd_buffer.source.ready.eq(req.wdata_ready | req.rdata_valid),
            req.lock.eq(cmd_buffer.source.valid),
        ]

        slicer = _AddressSlicer(settings.geom.colbits, address_align)

        # Row tracking
        has_openrow = Signal()
        openrow = Signal(settings.geom.rowbits, reset_less=True)
        hit = Signal()
        self.comb += hit.eq(openrow == slicer.row(cmd_buffer.source.adr))
        track_open = Signal()
        track_close = Signal()
        self.sync += \
            If(track_close,
                has_openrow.eq(0)
            ).Elif(track_open,
                has_openrow.eq(1),
                openrow.eq(slicer.row(cmd_buffer.source.adr))
            )

        # Four Activate Window
        activate = Signal()
        activate_allowed = Signal(reset=1)
        tfaw = settings.timing.tFAW
        if tfaw is not None:
            activate_count = Signal(max=tfaw)
            activate_window = Signal(tfaw)
            self.sync += activate_window.eq(Cat(activate, activate_window))
            for i in range(tfaw):
                next_activate_count = Signal(max=tfaw)
                self.comb += next_activate_count.eq(activate_count + activate_window[i])
                activate_count = next_activate_count
            self.comb += If(activate_count >=4, activate_allowed.eq(0))

        # Address generation
        sel_row_adr = Signal()
        self.comb += [
            cmd.ba.eq(n),
            If(sel_row_adr,
                cmd.a.eq(slicer.row(cmd_buffer.source.adr))
            ).Else(
                cmd.a.eq(slicer.col(cmd_buffer.source.adr))
            )
        ]

        # Respect write-to-precharge specification
        precharge_time = 2 + settings.timing.tWR - 1 + 1
        self.submodules.precharge_timer = WaitTimer(precharge_time)
        self.comb += self.precharge_timer.wait.eq(~(cmd.valid &
                                                    cmd.ready &
                                                    cmd.is_write))

        # Control and command generation FSM
        self.submodules.fsm = fsm = FSM()
        fsm.act("REGULAR",
            If(self.refresh_req,
                NextState("REFRESH")
            ).Elif(cmd_buffer.source.valid,
                If(has_openrow,
                    If(hit,
                        # Note: write-to-read specification is enforced by
                        # multiplexer
                        cmd.valid.eq(1),
                        If(cmd_buffer.source.we,
                            req.wdata_ready.eq(cmd.ready),
                            cmd.is_write.eq(1),
                            cmd.we.eq(1),
                        ).Else(
                            req.rdata_valid.eq(cmd.ready),
                            cmd.is_read.eq(1)
                        ),
                        cmd.cas.eq(1)
                    ).Else(
                        NextState("PRECHARGE")
                    )
                ).Else(
                    If(activate_allowed,
                        NextState("ACTIVATE")
                    )
                )
            )
        )
        fsm.act("PRECHARGE",
            # Note: we are presenting the column address, A10 is always low
            If(self.precharge_timer.done,
                cmd.valid.eq(1),
                If(cmd.ready,
                    NextState("TRP")
                ),
                cmd.ras.eq(1),
                cmd.we.eq(1),
                cmd.is_cmd.eq(1)
            ),
            track_close.eq(1)
        )
        fsm.act("ACTIVATE",
            activate.eq(1),
            sel_row_adr.eq(1),
            track_open.eq(1),
            cmd.valid.eq(1),
            cmd.is_cmd.eq(1),
            If(cmd.ready,
                NextState("TRCD")
            ),
            cmd.ras.eq(1)
        )
        fsm.act("REFRESH",
            If(self.precharge_timer.done,
                self.refresh_gnt.eq(1),
            ),
            track_close.eq(1),
            cmd.is_cmd.eq(1),
            If(~self.refresh_req,
                NextState("REGULAR")
            )
        )
        fsm.delayed_enter("TRP", "ACTIVATE", settings.timing.tRP-1)
        fsm.delayed_enter("TRCD", "REGULAR", settings.timing.tRCD-1)
