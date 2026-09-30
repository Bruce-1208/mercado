import socket
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex

def guarded_connect(self, address):
    if isinstance(address, tuple):
        host, port = address[:2]
        if host not in ('127.0.0.1', '::1', 'localhost') or port in (3306, 33060, 5000, 5001, 8011):
            raise OSError('AUDIT_NETWORK_BLOCKED: external/business connection disabled')
    elif isinstance(address, str):
        raise OSError('AUDIT_NETWORK_BLOCKED: unix socket disabled')
    return _original_connect(self, address)
socket.socket.connect = guarded_connect
