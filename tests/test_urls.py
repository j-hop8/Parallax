from parallax.urls import canonicalize


def test_strips_tracking_params_but_keeps_identifying_ones():
    # UDN-style: the story id is in the path, but some outlets carry an id in the
    # query string, so only known-tracking keys may be dropped.
    assert canonicalize("https://udn.com/news/story/7321/8123456?utm_source=fb&from=udn_ch2") == (
        "https://udn.com/news/story/7321/8123456"
    )
    assert canonicalize("https://www.setn.com/News.aspx?NewsID=1500000&utm_medium=x") == (
        "https://setn.com/News.aspx?NewsID=1500000"
    )


def test_same_article_across_polls_collapses_to_one_key():
    variants = [
        "https://news.ltn.com.tw/news/society/breakingnews/4700000",
        "http://www.news.ltn.com.tw/news/society/breakingnews/4700000/",
        "https://news.ltn.com.tw/news/society/breakingnews/4700000#comments",
        "https://news.ltn.com.tw/news/society/breakingnews/4700000?fbclid=abc123",
    ]
    keys = {canonicalize(v) for v in variants}
    # These are the forms one feed emits for a single article across polls:
    # scheme, www., trailing slash, fragment and tracking noise all vary. If any
    # of them produced a distinct key we would insert the same article twice and
    # inflate every count built on top of it.
    assert keys == {"https://news.ltn.com.tw/news/society/breakingnews/4700000"}


def test_query_param_order_does_not_change_the_key():
    a = canonicalize("https://www.chinatimes.com/realtimenews/x?b=2&a=1")
    b = canonicalize("https://www.chinatimes.com/realtimenews/x?a=1&b=2")
    assert a == b


def test_root_path_is_preserved():
    assert canonicalize("https://www.cna.com.tw/") == "https://cna.com.tw/"


def test_default_port_is_dropped_but_odd_port_is_kept():
    assert canonicalize("https://example.com:443/a") == "https://example.com/a"
    assert canonicalize("https://example.com:8443/a") == "https://example.com:8443/a"
