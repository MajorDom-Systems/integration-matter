parameters = [
    {
        'id': '00000000-0000-0000-0000-000000000000',
        'name': 'Toggle',
        'data_type': 'none',
        'unit': 'plain',
        'role': 'control',
        'visibility': 'setting',
        'min_value': None,
        'max_value': None,
        'min_step': None,
        'valid_values': None,
        'fields': [],
        'integration_data': {
            'endpoint_id': 13,
            'cluster_id': 6,
            'is_client': False,
            'command_id': 2,
            'attribute_id': None,
            'type': 'command'
        }
    },
    {
        'id': '00000000-0000-0000-0000-000000000001',
        'name': 'StartUpOnOff',
        'data_type': 'integer',
        'unit': 'plain',
        'role': 'control',
        'visibility': 'setting',
        'min_value': 0.0,
        'max_value': 255.0,
        'min_step': None,
        'valid_values': None,
        'fields': None,
        'integration_data': {
            'endpoint_id': 13,
            'cluster_id': 6,
            'is_client': False,
            'command_id': None,
            'attribute_id': 16387,
            'type': 'attribute'
        }
    },
    {
        'id': '00000000-0000-0000-0000-000000000002',
        'name': 'OnOff',
        'data_type': 'bool',
        'unit': 'plain',
        'role': 'sensor',
        'visibility': 'user',
        'min_value': None,
        'max_value': None,
        'min_step': None,
        'valid_values': None,
        'fields': None,
        'integration_data': {
            'endpoint_id': 13,
            'cluster_id': 6,
            'is_client': False,
            'command_id': None,
            'attribute_id': 0,
            'type': 'attribute'
        }
    },
    {
        'id': '00000000-0000-0000-0000-000000000003',
        'name': 'CurrentLevel',
        'data_type': 'integer',
        'unit': 'percentage',
        'role': 'sensor',
        'visibility': 'user',
        'min_value': 0.0,
        'max_value': 255.0,
        'min_step': 1.0,
        'valid_values': None,
        'fields': None,
        'integration_data': {
            'endpoint_id': 13,
            'cluster_id': 8,
            'is_client': False,
            'command_id': None,
            'attribute_id': 0,
            'type': 'attribute'
        }
    }
]