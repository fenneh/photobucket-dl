"""HTTP client for Photobucket's private GraphQL API + S3 downloader."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import requests

from photobucket_dl import queries

GRAPHQL_ENDPOINT = "https://app.photobucket.com/api/graphql/v2"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class AuthError(RuntimeError):
    """Auth cookie was rejected by the server."""


class GraphQLError(RuntimeError):
    pass


@dataclass
class Bucket:
    id: str
    title: str
    bucket_type: str | None
    total_media: int


@dataclass
class Album:
    id: str
    title: str
    parent_id: str | None
    bucket_id: str
    sub_album_count: int


@dataclass
class Media:
    id: str
    album_id: str | None
    filename: str
    original_filename: str | None
    title: str | None
    is_video: bool
    media_type: str | None
    file_size: int | None
    album_path: str = ""


class Client:
    """Talks to Photobucket's GraphQL API using a Firebase JWT cookie value."""

    def __init__(self, jwt: str) -> None:
        self.jwt = jwt
        self.session = requests.Session()
        self.session.headers.update({
            "authorization": jwt,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://app.photobucket.com",
            "Referer": "https://app.photobucket.com/",
            "User-Agent": USER_AGENT,
            "apollographql-client-name": "photobucket-web",
            "apollographql-client-version": "1.0.0",
        })
        # S3 presigned URLs reject requests carrying Photobucket Origin/Referer
        # headers, so we use a fresh session with just a User-Agent for the
        # actual binary downloads.
        self.s3 = requests.Session()
        self.s3.headers.update({"User-Agent": USER_AGENT})

    def gql(self, query: str, variables: dict, op_name: str) -> dict:
        payload = {"operationName": op_name, "query": query, "variables": variables}
        for attempt in range(5):
            r = self.session.post(GRAPHQL_ENDPOINT, json=payload, timeout=60)
            if r.status_code == 200:
                data = r.json()
                if data.get("errors"):
                    if any(
                        (e.get("extensions") or {}).get("code") in {"UNAUTHENTICATED", "FORBIDDEN"}
                        or "Unauthenticated" in (e.get("message") or "")
                        or "Unauthenticed" in (e.get("message") or "")
                        for e in data["errors"]
                    ):
                        raise AuthError(data["errors"])
                    raise GraphQLError(f"{op_name}: {data['errors']}")
                return data["data"]
            if r.status_code in (401, 403):
                raise AuthError(f"HTTP {r.status_code} from GraphQL endpoint")
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            raise GraphQLError(f"{op_name}: HTTP {r.status_code} {r.text[:200]}")
        raise GraphQLError(f"{op_name}: retries exhausted")

    def list_buckets(self, user_id: str) -> list[Bucket]:
        out: list[Bucket] = []
        token: str | None = None
        while True:
            d = self.gql(
                queries.BUCKETS_BY_USER_ID,
                {"userId": user_id, "nextToken": token, "limit": 50},
                "BucketsByUserId",
            )
            page = d["bucketsByUserId"]
            for b in page["items"]:
                out.append(Bucket(
                    id=b["id"],
                    title=b.get("title") or "",
                    bucket_type=b.get("bucketType"),
                    total_media=(b.get("counters") or {}).get("totalMedia") or 0,
                ))
            token = page.get("nextToken")
            if not token:
                return out

    def list_albums(self, bucket_id: str) -> list[Album]:
        """BFS the full album tree under a bucket."""
        seen: set[str] = set()
        out: list[Album] = []
        queue: list[str | None] = [None]
        while queue:
            parent = queue.pop(0)
            token: str | None = None
            while True:
                d = self.gql(
                    queries.BUCKET_ALBUMS,
                    {"bucketId": bucket_id, "albumId": parent, "nextToken": token},
                    "BucketAlbums",
                )
                page = d["bucketAlbums"]
                for a in page["items"]:
                    if a["id"] in seen:
                        continue
                    seen.add(a["id"])
                    out.append(Album(
                        id=a["id"],
                        title=a.get("title") or "",
                        parent_id=a.get("parentId"),
                        bucket_id=a.get("bucketId") or bucket_id,
                        sub_album_count=a.get("subAlbumCount") or 0,
                    ))
                    if (a.get("subAlbumCount") or 0) > 0:
                        queue.append(a["id"])
                token = page.get("nextToken")
                if not token:
                    break
        return out

    def list_media(self, bucket_id: str, album_id: str | None) -> list[dict]:
        items: list[dict] = []
        token: str | None = None
        while True:
            d = self.gql(
                queries.BUCKET_MEDIA_BY_ALBUM_ID,
                {
                    "bucketId": bucket_id,
                    "albumId": album_id,
                    "limit": 100,
                    "nextToken": token,
                },
                "BucketMediaByAlbumId",
            )
            page = d["bucketMediaByAlbumId"]
            items.extend(page["items"])
            token = page.get("nextToken")
            if not token:
                return items

    def fetch_signed_urls(self, bucket_id: str, media_ids: list[str]) -> dict[str, str]:
        """Return {media_id: presigned S3 URL} for the originals."""
        out: dict[str, str] = {}
        batch = 50
        for i in range(0, len(media_ids), batch):
            chunk = media_ids[i:i + batch]
            d = self.gql(
                queries.BUCKET_MEDIA_BY_IDS,
                {"bucketId": bucket_id, "mediaIds": chunk},
                "BucketMediaByIds",
            )
            for item in d["bucketMediaByIds"]:
                if item and item.get("signedUrl"):
                    out[item["id"]] = item["signedUrl"]
        return out

    def download(self, url: str, target: Path, expected_size: int | None = None) -> str:
        """Stream a presigned-URL download to disk.

        Returns one of: "ok", "skip", or raises on failure.
        Skips when target already exists and matches expected_size (or any size
        if expected_size is None).
        """
        if target.exists() and target.stat().st_size > 0:
            if expected_size is None or target.stat().st_size == expected_size:
                return "skip"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            with self.s3.get(url, stream=True, timeout=120) as r:
                if r.status_code != 200:
                    raise GraphQLError(f"HTTP {r.status_code}: {r.text[:200]}")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(64 * 1024):
                        if chunk:
                            f.write(chunk)
            tmp.replace(target)
            return "ok"
        except BaseException:
            if tmp.exists():
                tmp.unlink()
            raise
