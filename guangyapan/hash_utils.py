import hashlib
import os

def calculate_gcid_cid(file_path: str):
    """
    Calculate Xunlei/Guangyapan GCID and CID for a given file.
    Exact algorithm reverse-engineered from uploadHashWorker.cjs:
    - Block size:
        <= 128MB: 256KB
        128MB - 256MB: 512KB
        256MB - 512MB: 1MB
        > 512MB: 2MB
    - GCID: SHA-1 of the concatenated block SHA-1 hashes
    - CID: SHA-1 of the first 20KB, middle 20KB, and last 20KB (or full file if < 60KB)
    """
    file_size = os.path.getsize(file_path)
    if file_size <= 0x8000000:       # <= 128MB
        block_size = 262144          # 256KB
    elif file_size <= 0x10000000:    # <= 256MB
        block_size = 524288          # 512KB
    elif file_size <= 0x20000000:    # <= 512MB
        block_size = 1048576         # 1MB
    else:
        block_size = 2097152         # 2MB

    gcid_hasher = hashlib.sha1()
    cid_hasher = hashlib.sha1()

    if file_size < 61440:
        cid_ranges = [(0, file_size)]
    else:
        mid = file_size // 3
        cid_ranges = [
            (0, 20480),
            (mid, mid + 20480),
            (file_size - 20480, file_size)
        ]

    with open(file_path, 'rb') as f:
        offset = 0
        while offset < file_size:
            chunk_len = min(block_size, file_size - offset)
            chunk = f.read(chunk_len)
            if not chunk:
                break
            
            # GCID block hash
            block_sha1 = hashlib.sha1(chunk).digest()
            gcid_hasher.update(block_sha1)

            # CID range overlap check
            chunk_end = offset + len(chunk)
            for r_start, r_end in cid_ranges:
                overlap_start = max(offset, r_start)
                overlap_end = min(chunk_end, r_end)
                if overlap_start < overlap_end:
                    cid_hasher.update(chunk[overlap_start - offset : overlap_end - offset])

            offset += len(chunk)

    return {
        "gcid": gcid_hasher.hexdigest().upper(),
        "cid": cid_hasher.hexdigest().upper(),
        "fileSize": file_size
    }

def calculate_file_md5(file_path: str):
    hasher = hashlib.md5()
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(1048576) # 1MB
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()
