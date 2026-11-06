"""Opt-in aioquic adapter using public asyncio hooks and the native C ABI SDK."""
from aioquic.asyncio import QuicConnectionProtocol
from aioquic.quic.events import HandshakeCompleted, ConnectionTerminated


class CooperativeQuic(QuicConnectionProtocol):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.connection=args[0] if args else kwargs['quic']
        self.udp_socket=None
        self.handshake_confirmed=False
        self.termination=None

    def connection_made(self,transport):
        super().connection_made(transport)
        self.udp_socket=transport.get_extra_info('socket')

    def quic_event_received(self,event):
        if isinstance(event,HandshakeCompleted):
            self.handshake_confirmed=True
        elif isinstance(event,ConnectionTerminated):
            self.termination=dict(error_code=event.error_code,reason=event.reason_phrase)
        super().quic_event_received(event)

    def cancel_stream(self,stream_id):
        self.connection.reset_stream(stream_id,error_code=0x10c)
        self.transmit()

    def declare(self,service,session):
        # Blocking setup operation: call outside the application's request hot path.
        cookie=service.lib.fc_v2_associate(service.handle,self.udp_socket.fileno())
        if not cookie:
            raise RuntimeError('UDP descriptor association rejected')
        service.declare_transport(session,'quic',multiplexed=True,mobility=True,cookie=cookie)
        return int(cookie)
