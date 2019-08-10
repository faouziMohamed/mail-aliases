import base64
from urllib.parse import urlparse, parse_qs

from flask import url_for

from app.extensions import db
from app.jose_utils import verify_id_token
from app.models import Client, User
from app.oauth.views.authorize import (
    get_host_name_and_scheme,
    generate_access_token,
    construct_url,
)
from tests.utils import login


def test_get_host_name_and_scheme():
    assert get_host_name_and_scheme("http://localhost:8000?a=b") == (
        "localhost",
        "http",
    )

    assert get_host_name_and_scheme(
        "https://www.bubblecode.net/en/2016/01/22/understanding-oauth2/#Implicit_Grant"
    ) == ("www.bubblecode.net", "https")


def test_generate_access_token(flask_client):
    access_token = generate_access_token()
    assert len(access_token) == 40


def test_construct_url():
    url = construct_url("http://ab.cd", {"x": "1 2"})
    assert url == "http://ab.cd?x=1%202"


def test_authorize_page_non_login_user(flask_client):
    """make sure to display login page for non-authenticated user"""
    user = User.create("test@test.com", "test user")
    client = Client.create_new("test client", user.id)

    db.session.commit()

    r = flask_client.get(
        url_for(
            "oauth.authorize",
            client_id=client.oauth_client_id,
            state="teststate",
            redirect_uri="http://localhost",
            response_type="code",
        )
    )

    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "In order to accept the request, you need to login or sign up" in html


def test_authorize_page_login_user_non_supported_flow(flask_client):
    """return 400 if the flow is not supported"""
    user = login(flask_client)
    client = Client.create_new("test client", user.id)
    db.session.commit()

    # Not provide any flow
    r = flask_client.get(
        url_for(
            "oauth.authorize",
            client_id=client.oauth_client_id,
            state="teststate",
            redirect_uri="http://localhost",
            # not provide response_type param here
        )
    )

    # Provide a not supported flow
    html = r.get_data(as_text=True)
    assert r.status_code == 400
    assert "SimpleLogin only support the following OIDC flows" in html

    r = flask_client.get(
        url_for(
            "oauth.authorize",
            client_id=client.oauth_client_id,
            state="teststate",
            redirect_uri="http://localhost",
            # SL does not support this flow combination
            response_type="code token id_token",
        )
    )

    html = r.get_data(as_text=True)
    assert r.status_code == 400
    assert "SimpleLogin only support the following OIDC flows" in html


def test_authorize_page_login_user(flask_client):
    """make sure to display authorization page for authenticated user"""
    user = login(flask_client)
    client = Client.create_new("test client", user.id)

    db.session.commit()

    r = flask_client.get(
        url_for(
            "oauth.authorize",
            client_id=client.oauth_client_id,
            state="teststate",
            redirect_uri="http://localhost",
            response_type="code",
        )
    )

    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "You can customize the info sent to this app" in html
    assert "a@b.c (Personal Email)" in html


def test_authorize_code_flow_no_openid_scope(flask_client):
    """make sure the authorize redirects user to correct page for the *Code Flow*
    and when the *openid* scope is not present
    , ie when response_type=code, openid not in scope
    """

    user = login(flask_client)
    client = Client.create_new("test client", user.id)

    db.session.commit()

    # user allows client on the authorization page
    r = flask_client.post(
        url_for(
            "oauth.authorize",
            client_id=client.oauth_client_id,
            state="teststate",
            redirect_uri="http://localhost",
            response_type="code",
        ),
        data={"button": "allow", "suggested-email": "x@y.z", "suggested-name": "AB CD"},
        # user will be redirected to client page, do not allow redirection here
        # to assert the redirect url
        # follow_redirects=True,
    )

    assert r.status_code == 302  # user gets redirected back to client page

    # r.location will have this form http://localhost?state=teststate&code=knuyjepwvg
    o = urlparse(r.location)
    assert o.netloc == "localhost"
    assert not o.fragment

    # parse the query, should return something like
    # {'state': ['teststate'], 'code': ['knuyjepwvg']}
    queries = parse_qs(o.query)

    assert queries["state"] == ["teststate"]
    assert len(queries["code"]) == 1

    # Exchange the code to get access_token
    basic_auth_headers = base64.b64encode(
        f"{client.oauth_client_id}:{client.oauth_client_secret}".encode()
    ).decode("utf-8")

    r = flask_client.post(
        url_for("oauth.token"),
        headers={"Authorization": "Basic " + basic_auth_headers},
        data={"grant_type": "authorization_code", "code": queries["code"][0]},
    )

    # r.json should have this format
    # {
    #   'access_token': 'avmhluhonsouhcwwailydwvhankspptgidoggcbu',
    #   'expires_in': 3600,
    #   'scope': '',
    #   'token_type': 'bearer',
    #   'user': {
    #     'avatar_url': None,
    #     'client': 'test client',
    #     'email': 'x@y.z',
    #     'email_verified': True,
    #     'id': 1,
    #     'name': 'AB CD'
    #   }
    # }
    assert r.status_code == 200
    assert r.json["access_token"]
    assert r.json["expires_in"] == 3600
    assert r.json["scope"] == ""
    assert r.json["token_type"] == "bearer"

    assert r.json["user"] == {
        "avatar_url": None,
        "client": "test client",
        "email": "x@y.z",
        "email_verified": True,
        "id": 1,
        "name": "AB CD",
    }


def test_authorize_code_flow_with_openid_scope(flask_client):
    """make sure the authorize redirects user to correct page for the *Code Flow*
    and when the *openid* scope is present
    , ie when response_type=code, openid in scope

    The authorize endpoint should stay the same: return the *code*.
    The token endpoint however should now return id_token in addition to the access_token
    """

    user = login(flask_client)
    client = Client.create_new("test client", user.id)

    db.session.commit()

    # user allows client on the authorization page
    r = flask_client.post(
        url_for(
            "oauth.authorize",
            client_id=client.oauth_client_id,
            state="teststate",
            redirect_uri="http://localhost",
            response_type="code",
            scope="openid",  # openid is in scope
        ),
        data={"button": "allow", "suggested-email": "x@y.z", "suggested-name": "AB CD"},
        # user will be redirected to client page, do not allow redirection here
        # to assert the redirect url
        # follow_redirects=True,
    )

    assert r.status_code == 302  # user gets redirected back to client page

    # r.location will have this form http://localhost?state=teststate&code=knuyjepwvg
    o = urlparse(r.location)
    assert o.netloc == "localhost"
    assert not o.fragment

    # parse the query, should return something like
    # {'state': ['teststate'], 'code': ['knuyjepwvg']}
    queries = parse_qs(o.query)

    assert queries["state"] == ["teststate"]
    assert len(queries["code"]) == 1

    # Exchange the code to get access_token
    basic_auth_headers = base64.b64encode(
        f"{client.oauth_client_id}:{client.oauth_client_secret}".encode()
    ).decode("utf-8")

    r = flask_client.post(
        url_for("oauth.token"),
        headers={"Authorization": "Basic " + basic_auth_headers},
        data={"grant_type": "authorization_code", "code": queries["code"][0]},
    )

    # r.json should have this format
    # {
    #   'access_token': 'avmhluhonsouhcwwailydwvhankspptgidoggcbu',
    #   'expires_in': 3600,
    #   'scope': '',
    #   'token_type': 'bearer',
    #   'user': {
    #     'avatar_url': None,
    #     'client': 'test client',
    #     'email': 'x@y.z',
    #     'email_verified': True,
    #     'id': 1,
    #     'name': 'AB CD'
    #   }
    # }
    print(r.json)
    assert r.status_code == 200
    assert r.json["access_token"]
    assert r.json["expires_in"] == 3600
    assert r.json["scope"] == ""
    assert r.json["token_type"] == "bearer"

    assert r.json["user"] == {
        "avatar_url": None,
        "client": "test client",
        "email": "x@y.z",
        "email_verified": True,
        "id": 1,
        "name": "AB CD",
    }

    # id_token must be returned
    assert r.json["id_token"]

    # id_token must be a valid, correctly signed JWT
    assert verify_id_token(r.json["id_token"])
