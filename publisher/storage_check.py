import argparse
import uuid

from botocore.exceptions import ClientError

from .storage import Storage, digest


def check(game):
    storage = Storage(game)
    prefix = f"{'release' if game == 'apk' else game}/.checks/{uuid.uuid4().hex}"
    source, target = prefix + "/source", prefix + "/copy"
    data = b"resource-publisher-conditional-write-check"
    try:
        etag = storage.put(source, data, "application/octet-stream", absent=True)
        def rejected(action):
            try:
                action()
            except ClientError as error:
                if error.response["ResponseMetadata"]["HTTPStatusCode"] == 412:
                    return
                raise
            raise ValueError("Storage ignored a required publication condition")
        rejected(lambda: storage.put(source, b"wrong", "application/octet-stream", absent=True))
        rejected(lambda: storage.put(source, b"wrong", "application/octet-stream", etag='"wrong-etag"'))
        storage.verify(source, len(data), digest(data), etag)
        etag = storage.put(source, data + b"2", "application/octet-stream", etag=etag)
        rejected(lambda: storage.copy(source, '"wrong-etag"', target, digest(data), "application/octet-stream"))
        copied = storage.copy(source, etag, target, digest(data + b"2"), "application/octet-stream")
        storage.verify(target, len(data) + 1, digest(data + b"2"), copied)
        return {"conditionalPut": True, "conditionalCopy": True, "readback": True}
    finally:
        for key in (target, source):
            head = storage.head(key)
            if head:
                storage.delete(key, head["ETag"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("game", choices=("phigros", "rizline", "kyou", "apk"))
    print(check(parser.parse_args().game))
