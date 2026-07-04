"""core/urlnorm.py — the linking-key module (M1, DESIGN.md §5.2).

Real-world patterns required by §5.2: X share URLs, YouTube share links,
arXiv abs/pdf equivalence, GitHub deep paths, DOI forms.
"""
import pytest

from app.core import urlnorm


# --- normalize_url: basics ----------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    # scheme/host lowercasing, default port, fragment, trailing slash
    ("HTTPS://Example.COM:443/Path/", "https://example.com/Path"),
    ("http://example.com:80/a", "http://example.com/a"),
    ("http://example.com:8080/a", "http://example.com:8080/a"),
    ("https://example.com/page#section-2", "https://example.com/page"),
    ("https://example.com/", "https://example.com"),
    # www. stripping and host dot
    ("https://www.example.com/x", "https://example.com/x"),
    ("https://example.com./x", "https://example.com/x"),
    # query param sorting for a stable key
    ("https://example.com/?b=2&a=1", "https://example.com?a=1&b=2"),
])
def test_normalize_basics(raw, expected):
    assert urlnorm.normalize_url(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "not a url", "ftp://example.com/file", "mailto:a@b.c",
    "javascript:alert(1)", "https://", "http:///nohost",
    # shell snippets in conversation text — urlsplit only raises on lazy
    # .port/.hostname access (found by the first real-data sync)
    "http://localhost:$PORT/api",
    "https://example.com:eighty/x",
])
def test_normalize_rejects_non_http(raw):
    assert urlnorm.normalize_url(raw) is None


def test_normalize_drops_credentials():
    norm = urlnorm.normalize_url("https://user:hunter2@example.com/x")
    assert norm == "https://example.com/x"
    assert "hunter2" not in norm


# --- tracking params ----------------------------------------------------------

def test_utm_and_click_ids_stripped():
    raw = ("https://example.com/article?utm_source=tw&utm_medium=social"
           "&utm_campaign=c&fbclid=IwAB123&gclid=xyz&id=42")
    assert urlnorm.normalize_url(raw) == "https://example.com/article?id=42"


def test_x_share_url():
    # the canonical X share form: ?s=20&t=<share-tag>, twitter.com alias
    raw = "https://twitter.com/karpathy/status/1734659057938477174?s=20&t=AbCdEf123"
    assert urlnorm.normalize_url(raw) == "https://x.com/karpathy/status/1734659057938477174"


@pytest.mark.parametrize("raw", [
    "https://mobile.twitter.com/user/status/1?s=46",
    "https://mobile.x.com/user/status/1",
    "https://www.twitter.com/user/status/1",
])
def test_twitter_host_aliases(raw):
    assert urlnorm.normalize_url(raw) == "https://x.com/user/status/1"


def test_s_param_only_stripped_on_x():
    # `s` is a search query elsewhere (e.g. WordPress) — must survive
    assert urlnorm.normalize_url("https://example.com/?s=query") == "https://example.com?s=query"


def test_tracking_params_extendable_via_env(monkeypatch):
    monkeypatch.setenv("CAIRN_TRACKING_PARAMS", "spm, weird_tok")
    raw = "https://example.com/a?spm=1.2.3&weird_tok=x&keep=1"
    assert urlnorm.normalize_url(raw) == "https://example.com/a?keep=1"


# --- YouTube ------------------------------------------------------------------

def test_youtube_share_link():
    # youtu.be share with si tracking → canonical watch URL
    assert (urlnorm.normalize_url("https://youtu.be/dQw4w9WgXcQ?si=AbCdEfGh")
            == "https://youtube.com/watch?v=dQw4w9WgXcQ")


def test_youtube_mobile_and_timestamp():
    assert (urlnorm.normalize_url("https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=42s")
            == "https://youtube.com/watch?v=dQw4w9WgXcQ")


def test_youtube_share_equals_watch():
    a = urlnorm.normalize_url("https://youtu.be/dQw4w9WgXcQ?si=xyz&t=30")
    b = urlnorm.normalize_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert a == b


# --- arXiv --------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "https://arxiv.org/abs/2310.06825",
    "https://arxiv.org/abs/2310.06825v2",
    "https://arxiv.org/pdf/2310.06825.pdf",
    "https://arxiv.org/pdf/2310.06825v1.pdf",
    "https://arxiv.org/pdf/2310.06825",
    "https://arxiv.org/html/2310.06825v3",
    "https://www.arxiv.org/abs/2310.06825",
])
def test_arxiv_abs_pdf_equivalence(raw):
    assert urlnorm.normalize_url(raw) == "https://arxiv.org/abs/2310.06825"


def test_arxiv_old_style_id():
    assert (urlnorm.normalize_url("https://arxiv.org/pdf/cond-mat/0207270v3.pdf")
            == "https://arxiv.org/abs/cond-mat/0207270")


# --- GitHub repo keys ----------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "https://github.com/Anthropics/claude-code",
    "https://github.com/anthropics/claude-code.git",
    "https://github.com/anthropics/claude-code/tree/main/src",
    "https://github.com/anthropics/claude-code/blob/main/README.md#usage",
    "https://github.com/anthropics/claude-code/issues/123",
    "https://github.com/anthropics/claude-code/pull/4?diff=split",
])
def test_github_repo_key_from_deep_paths(raw):
    assert urlnorm.extract_github_repo(raw) == "github.com/anthropics/claude-code"


@pytest.mark.parametrize("raw", [
    "https://github.com/anthropics",           # owner page, no repo
    "https://github.com/features/copilot",     # site page, not an owner
    "https://github.com/topics/sqlite",
    "https://gist.github.com/user/abc123",     # gists are not repos
    "https://example.com/anthropics/claude-code",
    None,
])
def test_github_repo_key_rejects_non_repos(raw):
    assert urlnorm.extract_github_repo(raw) is None


# --- DOI ------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "10.1038/s41586-023-06004-9",
    "DOI:10.1038/s41586-023-06004-9",
    "doi: 10.1038/s41586-023-06004-9",
    "https://doi.org/10.1038/s41586-023-06004-9",
    "http://dx.doi.org/10.1038/S41586-023-06004-9",
])
def test_doi_forms(raw):
    assert urlnorm.normalize_doi(raw) == "10.1038/s41586-023-06004-9"


def test_doi_percent_encoded_url():
    assert (urlnorm.normalize_doi("https://doi.org/10.1002/%28SICI%291097-4571")
            == "10.1002/(sici)1097-4571")


@pytest.mark.parametrize("raw", [None, "", "not-a-doi", "11.1234/x", "10.1234", "https://example.com/10.1/x"])
def test_doi_rejects_invalid(raw):
    assert urlnorm.normalize_doi(raw) is None


# --- extract_urls ----------------------------------------------------------------

def test_extract_urls_from_prose_and_markdown():
    text = ("見て https://example.com/a. それと [repo](https://github.com/o/r) と\n"
            "<https://example.com/b>、https://example.com/c、末尾。")
    assert urlnorm.extract_urls(text) == [
        "https://example.com/a",
        "https://github.com/o/r",
        "https://example.com/b",
        "https://example.com/c",
    ]


def test_extract_urls_keeps_balanced_parens():
    text = "wiki: https://en.wikipedia.org/wiki/RRF_(information_retrieval) 参照"
    assert urlnorm.extract_urls(text) == [
        "https://en.wikipedia.org/wiki/RRF_(information_retrieval)"
    ]


def test_extract_urls_dedupes_preserving_order():
    text = "https://a.example/x https://b.example/y https://a.example/x"
    assert urlnorm.extract_urls(text) == ["https://a.example/x", "https://b.example/y"]


# --- url_keys --------------------------------------------------------------------

def test_url_keys_composite():
    norm, doi, gh = urlnorm.url_keys("https://github.com/o/r/tree/main?utm_source=x")
    assert norm == "https://github.com/o/r/tree/main"
    assert doi is None
    assert gh == "github.com/o/r"

    norm, doi, gh = urlnorm.url_keys("https://doi.org/10.1000/XYZ?utm_source=mail")
    assert doi == "10.1000/xyz"
    assert gh is None
