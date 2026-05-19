"""GraphQL operations used against Photobucket's private app API.

These were extracted from the compiled JS bundle at app.photobucket.com.
If Photobucket changes the schema, these queries are what will need updating.
"""

BUCKETS_BY_USER_ID = """
query BucketsByUserId($userId: ID!, $nextToken: String, $limit: Int) {
  bucketsByUserId(userId: $userId, nextToken: $nextToken, limit: $limit) {
    nextToken
    items {
      id
      title
      ownerId
      bucketType
      counters { totalMedia totalSize totalAlbumCount }
    }
  }
}
"""

BUCKET_ALBUMS = """
query BucketAlbums($bucketId: ID!, $albumId: ID, $nextToken: String) {
  bucketAlbums(bucketId: $bucketId, albumId: $albumId, nextToken: $nextToken) {
    nextToken
    items {
      id
      title
      parentId
      bucketId
      subAlbumCount
      counters { totalSubalbums totalMedia }
    }
  }
}
"""

BUCKET_MEDIA_BY_ALBUM_ID = """
query BucketMediaByAlbumId(
  $bucketId: ID!, $albumId: ID, $limit: Int!, $nextToken: String
) {
  bucketMediaByAlbumId(
    bucketId: $bucketId
    albumId: $albumId
    limit: $limit
    nextToken: $nextToken
  ) {
    nextToken
    items {
      id
      albumId
      filename
      originalFilename
      title
      imageUrl
      isVideo
      mediaType
      fileSize
      width
      height
      createdAt
      dateTaken
    }
  }
}
"""

BUCKET_MEDIA_BY_IDS = """
query BucketMediaByIds($bucketId: ID!, $mediaIds: [ID!]!) {
  bucketMediaByIds(bucketId: $bucketId, mediaIds: $mediaIds) {
    id
    signedUrl
    originalFilename
    filename
    fileSize
  }
}
"""
