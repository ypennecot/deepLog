#!/usr/bin/env python3
"""
Module de décodage des messages USBL Kogger.
Décode les messages binaires depuis les logs CSV USBL.
"""

import re
import struct
from typing import Optional, Dict, Any

# Mapping des états Kogger vers Seaker (basé sur le code fourni)
KOGGER_TO_SEAKER_STATE_MAP = {
    0: 25,   # STANDBY
    1: 7,    # DEPTH_HOLD
    2: 11,   # ALT_HOLD
    3: 13,   # DEPTH_HOLD_FOLLOW ou HOMING
    4: 14,   # ALT_HOLD_FOLLOW
    5: 19,   # SURFACE
    6: 22,   # RECOVER_STUCK
    7: 1,    # EMERGENCY_BATTERY (peut aussi être 3, 4, 8)
    8: 16,   # EMERGENCY_STUCK
    255: 0   # NOTHING
}

# Commandes USBL identifiées
CMD_POSITION_REQUEST = 0x8A
CMD_POSITION_RESPONSE = 0xC9
CMD_STATUS_RESPONSE = 0x41
CMD_CONFIG_ACK = 0xE1
CMD_CONFIG_RESPONSE = 0xD9
CMD_COMMAND_ACK = 0xE9


def parse_bytes_from_string(bytes_str: str) -> Optional[bytes]:
    """
    Parse les bytes depuis le format Python b'...'
    
    Args:
        bytes_str: String au format b'\\xbb\\x55...'
    
    Returns:
        bytes object ou None si erreur
    """
    try:
        # Extraire le contenu entre b'...'
        match = re.search(r"b'(.+?)'", bytes_str)
        if not match:
            return None
        
        content = match.group(1)
        # Convertir les séquences d'échappement Python en bytes
        return content.encode('latin-1').decode('unicode_escape').encode('latin-1')
    except Exception as e:
        print(f"Error parsing bytes: {e}")
        return None


def identify_message_type(data: bytes) -> Optional[int]:
    """
    Identifie le type de message USBL.
    
    Args:
        data: Bytes du message
    
    Returns:
        Code de commande (0x8A, 0xC9, etc.) ou None
    """
    if len(data) < 3:
        return None
    
    # Format: BB 55 [LEN] [CMD] ... pour SENT
    if data[0] == 0xBB and len(data) >= 4:
        if data[1] == 0x55:
            return data[3]  # Commande à l'offset 3
    
    # Format: 55 [LEN] [CMD] ... pour RECEIVED
    if data[0] == 0x55 and len(data) >= 3:
        return data[2]  # Commande à l'offset 2
    
    # ACK simple
    if data[0] == 0xBB and len(data) == 1:
        return 0xBB  # ACK
    
    return None


def decode_position_response(data: bytes) -> Optional[Dict[str, Any]]:
    """
    Décode un message POSITION_RESPONSE (0xC9).
    
    Format observé: 55 00 C9 68 03 01 [SEQ] [DATA] [CHK]
    
    Note: Les messages 0xC9 observés sont très courts (10 bytes) et ne contiennent
    que 2 bytes de données. Les données complètes (azimuth, elevation, distance)
    sont probablement dans les messages STATUS_RESPONSE (0x41) plus longs, ou
    nécessitent un décodage différent du protocole SBP.
    
    Pour l'instant, on extrait la séquence et les données brutes.
    Le décodage complet nécessiterait le driver Kogger ou la documentation SBP.
    
    Args:
        data: Bytes du message
    
    Returns:
        Dictionnaire avec les données décodées ou None
    """
    if len(data) < 10:
        return None
    
    # Vérifier que c'est bien un POSITION_RESPONSE
    if data[0] != 0x55 or data[2] != CMD_POSITION_RESPONSE:
        return None
    
    length = data[1]
    seq = data[6] if len(data) > 6 else None
    
    # Les données de position sont dans les bytes suivants
    # Format exact à déterminer - pour l'instant on extrait les bytes bruts
    data_bytes = data[7:-1] if len(data) > 8 else data[7:]
    checksum = data[-1] if len(data) > 7 else None
    
    result = {
        'message_type': 'POSITION_RESPONSE',
        'length': length,
        'sequence': seq,
        'data_bytes': list(data_bytes),
        'data_hex': ' '.join(f'{b:02X}' for b in data_bytes),
        'checksum': checksum,
        'raw_message': ' '.join(f'{b:02X}' for b in data),
        # Placeholders pour les valeurs décodées (à remplir quand le format sera connu)
        'azimuth_deg': None,
        'elevation_deg': None,
        'distance_m': None,
        'auv_state': None,
        'snr': None
    }
    
    # Note: Le format exact nécessite le driver Kogger ou la documentation SBP
    # Pour l'instant, on stocke les données brutes pour analyse ultérieure
    
    return result


def decode_status_response(data: bytes) -> Optional[Dict[str, Any]]:
    """
    Décode un message STATUS_RESPONSE (0x41).
    
    Format observé: 55 00 41 [DATA...] (32 bytes de données)
    
    Args:
        data: Bytes du message
    
    Returns:
        Dictionnaire avec les données décodées ou None
    """
    if len(data) < 4:
        return None
    
    if data[0] != 0x55 or data[2] != CMD_STATUS_RESPONSE:
        return None
    
    length = data[1]
    data_bytes = data[3:] if len(data) > 3 else []
    
    return {
        'message_type': 'STATUS_RESPONSE',
        'length': length,
        'data_bytes': list(data_bytes),
        'data_hex': ' '.join(f'{b:02X}' for b in data_bytes),
        'raw_message': ' '.join(f'{b:02X}' for b in data)
    }


def convert_kogger_state_to_seaker(kogger_state: int) -> int:
    """
    Convertit un état Kogger en état Seaker.
    
    Args:
        kogger_state: État Kogger (0-8, 255)
    
    Returns:
        État Seaker correspondant
    """
    return KOGGER_TO_SEAKER_STATE_MAP.get(kogger_state, 0)


def decode_usbl_message(data_str: str, direction: str) -> Optional[Dict[str, Any]]:
    """
    Décode un message USBL depuis une ligne de log CSV.
    
    Args:
        data_str: String contenant les bytes au format b'...'
        direction: 'SENT' ou 'RECEIVED'
    
    Returns:
        Dictionnaire avec les données décodées ou None
    """
    data = parse_bytes_from_string(data_str)
    if data is None:
        return None
    
    msg_type = identify_message_type(data)
    if msg_type is None:
        return {
            'message_type': 'UNKNOWN',
            'direction': direction,
            'raw_data': ' '.join(f'{b:02X}' for b in data),
            'length': len(data)
        }
    
    result = {
        'direction': direction,
        'command': f'0x{msg_type:02X}',
        'raw_data': ' '.join(f'{b:02X}' for b in data),
        'length': len(data)
    }
    
    # Décoder selon le type de message
    if msg_type == CMD_POSITION_RESPONSE:
        decoded = decode_position_response(data)
        if decoded:
            result.update(decoded)
    elif msg_type == CMD_STATUS_RESPONSE:
        decoded = decode_status_response(data)
        if decoded:
            result.update(decoded)
    elif msg_type == 0xBB:
        result['message_type'] = 'ACK'
    else:
        result['message_type'] = f'CMD_0x{msg_type:02X}'
    
    return result
