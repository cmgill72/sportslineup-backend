from app.normalization import merge_channels


def test_authorized_network_logo_survives_channel_dedup():
    first=[{'channel':'ABC','source':'TheSportsDB','confidence':'provider_listed'}]
    licensed=[{'channel':'ABC','source':'Authorized feed','logoURL':'https://partner.example/abc.png'}]
    merge_channels(first,licensed)
    assert len(first)==1
    assert first[0]['logoURL']=='https://partner.example/abc.png'
    assert 'Authorized feed' in first[0]['sources']
