# Verify your Kraken key cannot withdraw

Run this inside the container after setting your keys. All three probes must
say **Permission denied** — anything else means the key is over-scoped and you
should revoke it and create a new one with query + trade permissions only.

    docker exec magpie python -c "
    from app import market
    ex = market.exchange()
    print('balance:', ex.fetch_balance().get('total', {}).get('EUR'))
    for name, call in [
        ('WithdrawMethods',   lambda: ex.private_post_withdrawmethods({'asset': 'XBT'})),
        ('WithdrawAddresses', lambda: ex.private_post_withdrawaddresses({})),
        ('WithdrawInfo',      lambda: ex.private_post_withdrawinfo({'asset': 'XBT', 'key': 'probe', 'amount': '0.001'}))]:
        try:
            call(); print(name, '-> ALLOWED (BAD: key can withdraw — revoke it)')
        except Exception as e:
            print(name, '->', str(e)[:70])
    "

Note the error semantics: `EGeneral:Permission denied` = correctly scoped;
`EAPI:Invalid key` = the key itself is wrong or IP-blocked (not a scope issue).
