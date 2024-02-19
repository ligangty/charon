"""
Copyright (C) 2022 Red Hat, Inc. (https://github.com/Commonjava/charon)

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

         http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from charon.utils.files import digest
from typing import Tuple, List, Dict
from bs4 import BeautifulSoup
import tempfile
import os
import logging
import requests
import shutil

logger = logging.getLogger(__name__)

DEFAULT_ARTIFACT_TYPES = ['.pom', '.jar', '.war', '.ear', '.zip', '.tar', '.gz', '.xml']


def handle_checksum_validation_http(
    bucket: str,
    path: str,
    includes: str,
    report_file_path: str,
    recursive: bool = False,
    skips: List[str] = None
):
    """ Handle the checksum check for maven artifacts.
        * target contains bucket name and prefix for the bucket, which will
          be used to store artifacts with the prefix. See target definition
          in Charon configuration for details.
        * path is the root path where to start the validation in the bucket.
        * includes are the file suffixes which will decide the types of files
          to do the validation.
        * recursive decide if to validate the path recursively, default false.
          Becareful to set true because it will be very time-consuming to do the
          recursive validation as it will recursively scan all sub paths in
          the path.

        This will generate a file contains all artifacts which mismatched with its
        checksum files. Will use sha1 to do the validation.
    """
    local_dir = tempfile.mkdtemp()
    results = ([], [], [])
    try:
        if not os.path.exists(local_dir):
            os.makedirs(local_dir)
        root_url = _decide_root_url(bucket)
        logger.debug("Root url is %s", root_url)
        _collect_invalid_files(
            root_url, path, includes, local_dir, recursive, skips, results
        )
    finally:
        shutil.rmtree(local_dir)
        if results and any([
            results[0] and len(results[0]) > 0,
            results[1] and len(results[1]) > 0,
            results[2] and len(results[2]) > 0
        ]):
            _gen_report(report_file_path, results)


def _collect_invalid_files(
    root_url: str,
    path: str,
    includes: str,
    work_dir: str,
    recursive: bool,
    skips: List[str],
    results: Tuple[List[str], List[str], List[Dict[str, str]]]
):
    if skips and path in skips:
        logger.info("Path %s is in skips list, will not check it", path)
        return
    logger.info("Validating path %s", path)

    try:
        folder_url = os.path.join(root_url, path)
        items = _list_folder_content(folder_url, path)
        sub_folders = [item for item in items if item.endswith("/")]
        files = [item for item in items if not item.endswith("/")]
        if path+"/" in sub_folders:
            sub_folders.remove(path+"/")
        logger.debug("Folders in path %s: %s", path, sub_folders)
        logger.debug("Files in path %s: %s", path, files)
        include_types = DEFAULT_ARTIFACT_TYPES
        if includes and includes.strip() != "":
            include_types = includes.split(",")
        for f in files:
            if any(f.endswith(filetype) for filetype in include_types):
                _do_validation(root_url, f, work_dir, results)
    except Exception as e:
        logger.error("Error happened during checking path %s: %s", path, e)
    if recursive:
        for folder in sub_folders:
            _collect_invalid_files(root_url, folder, includes, work_dir, recursive, skips, results)


def _do_validation(
    root_url: str, file: str, work_dir: str,
    results: Tuple[List[str], List[str], List[Dict[str, str]]]
):
    mismatch_files = results[0]
    missing_checksum_files = results[1]
    error_files = results[2]
    item_path = file
    checksum_file_url = os.path.join(root_url, item_path + ".sha1")
    checksum = None
    if not _remote_file_exists(checksum_file_url):
        logger.info("Missing checksum file for file %s", item_path)
        missing_checksum_files.append(item_path)
    else:
        local_path = os.path.join(work_dir, item_path)
        try:
            # At first we want to get checksum from s3 metadata for files, but found it
            # does not match with the file itself after checking. So here we download
            # the file itself and do digesting directly
            _download_file(root_url, item_path, work_dir)
            checksum = digest(local_path)
        except Exception as e:
            logger.error("Validation failed for file %s: %s", item_path, e)
            error_files.append({"path": item_path, "error": str(e)})
        finally:
            if os.path.exists(local_path):
                os.remove(local_path)
        if checksum and checksum.strip() != "":
            remote_checksum = _read_remote_file_content(checksum_file_url)
            if remote_checksum is None:
                logger.info("Missing checksum file for file %s", item_path)
                missing_checksum_files.append(item_path)
            elif checksum.strip().lower() != remote_checksum.strip().lower():
                logger.info("""Found mismatched file %s, file checksum %s,
                            remote checksum: %s""", item_path, checksum, remote_checksum)
                mismatch_files.append(item_path)


def _gen_report(
    report_file_path: str,
    content: Tuple[List[str], List[str], List[Dict[str, str]]]
):
    """Generate a report file."""
    work_dir = report_file_path
    if work_dir and work_dir.strip() != "":
        if not os.path.isdir(work_dir):
            tmp_dir = tempfile.gettempdir()
            work_dir = os.path.join(tmp_dir, work_dir)
            if not os.path.isdir(work_dir):
                os.makedirs(work_dir)
                logger.debug("Created %s as report file directory.", work_dir)
    else:
        work_dir = tempfile.mkdtemp()
        logger.debug("""The report file path is empty.
                    Created temp dir %s as report file path.""", work_dir)

    def _check_and_remove_file(file_name: str):
        if os.path.isfile(file_name):
            os.remove(file_name)

    def _write_one_col_file(items: List[str], file_name: str):
        if items and len(items) > 0:
            _check_and_remove_file(file_name)
            with open(file_name, "w") as f:
                for i in items:
                    f.write(i + "\n")
            logger.info("The report file %s is generated.", file_name)

    _write_one_col_file(content[0], os.path.join(work_dir, "mismatched_files.csv"))
    _write_one_col_file(content[1], os.path.join(work_dir, "missing_checksum_files.csv"))

    if content[2] and len(content[2]) > 0:
        error_file = os.path.join(work_dir, "error_files.csv")
        _check_and_remove_file(error_file)
        with open(error_file, "w") as f:
            f.write("path,error\n")
            for d in content[2]:
                f.write("{path},{error}\n".format(path=d["path"], error=d["error"]))
        logger.info("The report file %s is generated.", error_file)


def _remote_file_exists(file_url: str) -> bool:
    with requests.head(file_url) as r:
        if r.status_code == 200:
            return True
    return False


def _download_file(root_url: str, file_path: str, work_dir: str):
    file_url = os.path.join(root_url, file_path)
    logger.debug("Start downloading file %s", file_url)
    local_filename = os.path.join(work_dir, file_path)
    local_dir = os.path.dirname(local_filename)
    if not os.path.exists(local_dir):
        logger.debug("Creating dir %s", local_dir)
        os.makedirs(local_dir)
    # NOTE the stream=True parameter below
    try:
        with requests.get(file_url, stream=True) as r:
            if r.status_code == 200:
                with open(local_filename, 'wb') as f:
                    # shutil.copyfileobj(r.raw, f)
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
        logger.debug("Downloaded file %s to %s", file_path, local_filename)
    except Exception as e:
        logger.error("Download file %s failed: %s", file_path, e)
        raise e
    return local_filename


def _list_folder_content(folder_url: str, folder_path: str) -> List[str]:
    try:
        with requests.get(folder_url) as r:
            if r.status_code == 200:
                contentType = r.headers.get('Content-Type')
                if contentType and "text/html" in contentType:
                    pageContent = r.text
                    return _parseContent(pageContent, folder_path)
                else:
                    logger.warning("%s is not a folder!", folder_url)
    except Exception as e:
        logger.error("Can not list folder %s. The error is %s", folder_url, e)
    return []


def _parseContent(pageContent: str, parent: str) -> List[str]:
    items = []
    soup = BeautifulSoup(pageContent, "html.parser")
    contents = soup.find("ul", id="contents").find_all("a")
    for c in contents:
        item = c["href"]
        if not item or item.strip() == '../':
            continue
        items.append(os.path.join(parent, item))
    return items


def _read_remote_file_content(remote_file_url: str) -> str:
    try:
        with requests.get(remote_file_url) as r:
            if r.status_code == 200:
                return r.text.strip() if r.text else ""
    except Exception as e:
        logger.error("Can not read file %s. The error is %s", remote_file_url, e)
        return None


def _decide_root_url(bucket: str) -> str:
    if bucket.strip().startswith("prod-maven"):
        return "https://maven.repository.redhat.com"
    if bucket.strip().startswith("stage-maven"):
        return "https://maven.stage.repository.redhat.com"
    return None
