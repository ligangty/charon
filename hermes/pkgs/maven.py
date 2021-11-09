"""
Copyright (C) 2021 Red Hat, Inc. (https://github.com/Commonjava/hermes)

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
import hermes.pkgs.indexing as indexing
from hermes.utils.files import write_file
from hermes.utils.archive import extract_zip_all
from hermes.storage import S3Client
from hermes.config import AWS_DEFAULT_BUCKET, get_template
from hermes.constants import META_FILE_GEN_KEY, META_FILE_DEL_KEY, MAVEN_METADATA_TEMPLATE
from typing import Dict, List, Tuple
from jinja2 import Template
from datetime import datetime
from zipfile import ZipFile
from tempfile import mkdtemp
import os
import sys
import logging
import re


logger = logging.getLogger(__name__)


def __get_mvn_template() -> str:
    """Gets the jinja2 template file content for maven-metadata.xml generation"""
    try:
        return get_template("maven-metadata.xml.j2")
    except FileNotFoundError:
        logger.info("maven-metadata.xml template file not defined,"
                    " will use default template.")
        return MAVEN_METADATA_TEMPLATE


META_TEMPLATE = __get_mvn_template()


class MavenMetadata(object):
    """This MavenMetadata will represent a maven-metadata.xml data content which will be
    used in jinja2 or other places
    """

    def __init__(self, group_id: str, artifact_id: str, versions: List[str]):
        self.group_id = group_id
        self.artifact_id = artifact_id
        self.last_upd_time = datetime.now().strftime("%Y%m%d%H%M%S")
        self.versions = sorted(set(versions), key=ver_cmp_key())
        self._latest_version = None
        self._release_version = None

    def generate_meta_file_content(self) -> str:
        template = Template(META_TEMPLATE)
        return template.render(meta=self)

    @property
    def latest_version(self):
        if self._latest_version:
            return self._latest_version
        self._latest_version = self.versions[-1]
        return self._latest_version

    @property
    def release_version(self):
        if self._release_version:
            return self._release_version
        self._release_version = self.versions[-1]
        return self._release_version

    def __str__(self) -> str:
        return f"{self.group_id}:{self.artifact_id}\n{self.versions}\n\n"


def scan_for_poms(full_path: str) -> List[str]:
    """Scan a file path and finds all pom files absolute paths"""
    # collect poms
    all_pom_paths = list()
    for (directory, _, names) in os.walk(full_path):
        single_pom_paths = [
            os.path.join(directory, n) for n in names if n.endswith(".pom")
        ]
        all_pom_paths.extend(single_pom_paths)
    return all_pom_paths


def parse_ga(full_ga_path: str, root="/") -> Tuple[str, str]:
    """Parse maven groupId and artifactId from a standard path in a local maven repo.
    e.g: org/apache/maven/plugin/maven-plugin-plugin -> (org.apache.maven.plugin,
                                                         maven-plugin-plugin)
    root is like a prefix of the path which is not part of the maven GAV
    """
    slash_root = root
    if not root.endswith("/"):
        slash_root = slash_root + "/"

    ga_path = full_ga_path
    if ga_path.startswith(slash_root):
        ga_path = ga_path[len(slash_root):]
    if ga_path.endswith("/"):
        ga_path = ga_path[:-1]

    items = ga_path.split("/")
    artifact = items[len(items) - 1]
    group = ".".join(items[:-1])

    return group, artifact


def __parse_gav(full_artifact_path: str, root="/") -> Tuple[str, str, str]:
    """Parse maven groupId, artifactId and version from a standard path in a local maven repo.
    e.g: org/apache/maven/plugin/maven-plugin-plugin/1.0.0/maven-plugin-plugin-1.0.0.pom
    -> (org.apache.maven.plugin, maven-plugin-plugin, 1.0.0)
    root is like a prefix of the path which is not part of the maven GAV
    """
    slash_root = root
    if not root.endswith("/"):
        slash_root = slash_root + "/"

    ver_path = full_artifact_path
    if ver_path.startswith(slash_root):
        ver_path = ver_path[len(slash_root):]
    if ver_path.endswith("/"):
        ver_path = ver_path[:-1]

    items = ver_path.split("/")
    version = items[-2]
    artifact = items[-3]
    group = ".".join(items[:-3])

    return group, artifact, version


def parse_gavs(pom_paths: List[str], root="/") -> Dict[str, Dict[str, List[str]]]:
    """Give a list of paths with pom files and parse the maven groupId, artifactId and version
    from them. The result will be a dict like {groupId: {artifactId: [versions list]}}.
    Root is like a prefix of the path which is not part of the maven GAV
    """
    gavs = dict()
    for pom in pom_paths:
        (g, a, v) = __parse_gav(pom, root)
        avs = gavs.get(g, dict())
        vers = avs.get(a, list())
        vers.append(v)
        avs[a] = vers
        gavs[g] = avs
    return gavs


def gen_meta_file(group_id, artifact_id: str, versions: list, root="/") -> str:
    content = MavenMetadata(
        group_id, artifact_id, versions
    ).generate_meta_file_content()
    g_path = "/".join(group_id.split("."))
    final_meta_path = os.path.join(root, g_path, artifact_id, "maven-metadata.xml")
    try:
        write_file(final_meta_path, content)
    except FileNotFoundError as e:
        logger.error(
            "Error: Can not create file %s because of some missing folders",
            final_meta_path,
        )
        raise e
    return final_meta_path


def handle_maven_uploading(
    repo: str,
    prod_key: str,
    ignore_patterns=None,
    root="maven-repository",
    bucket_name=None,
    dir_=None,
    do_index=True
):
    """ Handle the maven product release tarball uploading process.
        * repo is the location of the tarball in filesystem
        * prod_key is used to identify which product this repo
          tar belongs to
        * ga is used to identify if this is a GA product release
        * ignore_patterns is used to filter out paths which don't
          need to upload in the tarball
        * root is a prefix in the tarball to identify which path is
          the beginning of the maven GAV path
        * bucket_name is the s3 bucket name to store the artifacts
        * dir_ is base dir for extracting the tarball, will use system
          tmp dir if None.
    """
    # 1. extract tarball
    tmp_root = _extract_tarball(repo, prod_key, dir__=dir_)

    # 2. scan for paths and filter out the ignored paths,
    # and also collect poms for later metadata generation
    (top_level,
     valid_mvn_paths,
     valid_poms) = _scan_paths(tmp_root, ignore_patterns, root)

    # This prefix is a subdir under top-level directory in tarball
    # or root before real GAV dir structure
    if not os.path.isdir(top_level):
        logger.error("Error: the extracted top-level path %s does not exist.", top_level)
        sys.exit(1)

    # 3. do validation for the files, like product version checking
    logger.info("Validating paths with rules.")
    (err_msgs, passed) = _validate_maven(valid_mvn_paths)
    if not passed:
        _handle_error(err_msgs)
        # Question: should we exit here?

    # 4. Do uploading
    logger.info("Start uploading files to s3")
    s3_client = S3Client()
    bucket = bucket_name if bucket_name else AWS_DEFAULT_BUCKET
    uploaded_files = []
    _uploaded_files, failed_files = s3_client.upload_files(
        file_paths=valid_mvn_paths, bucket_name=bucket, product=prod_key, root=top_level
    )
    uploaded_files.extend(_uploaded_files)
    logger.info("Files uploading done\n")

    # 5. Use uploaded poms to scan s3 for metadata refreshment
    logger.info("Start generating maven-metadata.xml files for all artifacts")
    meta_files = _generate_metadatas(s3_client, bucket, valid_poms, top_level)
    logger.info("maven-metadata.xml files generation done\n")

    failed_metas = []
    # 6. Upload all maven-metadata.xml
    if META_FILE_GEN_KEY in meta_files:
        logger.info("Start updating maven-metadata.xml to s3")
        _uploaded_files, _failed_metas = s3_client.upload_metadatas(
            meta_file_paths=meta_files[META_FILE_GEN_KEY],
            bucket_name=bucket,
            product=prod_key,
            root=top_level
        )
        failed_metas.extend(_failed_metas)
        uploaded_files.extend(_uploaded_files)
        logger.info("maven-metadata.xml updating done\n")

    # this step generates index.html for each dir and add them to file list
    # index is similar to metadata, it will be overwritten everytime
    if do_index:
        logger.info("Start generating index files to s3")
        index_files = uploaded_files
        if META_FILE_GEN_KEY in meta_files:
            index_files = index_files + meta_files[META_FILE_GEN_KEY]
        created_files = indexing.handle_create_index(top_level, index_files, s3_client, bucket)
        logger.info("Index files generation done.\n")
        logger.info("Start updating index files to s3")
        _uploaded_files, _failed_metas = s3_client.upload_metadatas(
            meta_file_paths=created_files, bucket_name=bucket, product=None, root=top_level
        )
        failed_metas.extend(_failed_metas)
        logger.info("Index files updating done\n")
    else:
        logger.info("Bypass indexing")

    if len(failed_files) == 0 and len(failed_metas) == 0:
        logger.info("Product release %s is successfully"
                    " uploaded to Mercury service.", prod_key)
    else:
        logger.warning("Product release %s is uploaded to Mercury"
                       " service, but has some failure as below: \n",
                       prod_key)
        if len(failed_files) > 0:
            logger.warning("Files failed to upload: \n%s", failed_files)
        if len(failed_metas) > 0:
            logger.warning("Metadata files failed to refresh: \n%s",
                           failed_metas)


def handle_maven_del(
    repo: str,
    prod_key: str,
    ignore_patterns=None,
    root="maven-repository",
    bucket_name=None,
    dir_=None,
    do_index=True
):
    """ Handle the maven product release tarball deletion process.
        * repo is the location of the tarball in filesystem
        * prod_key is used to identify which product this repo
          tar belongs to
        * ga is used to identify if this is a GA product release
        * ignore_patterns is used to filter out paths which don't
          need to upload in the tarball
        * root is a prefix in the tarball to identify which path is
          the beginning of the maven GAV path
        * bucket_name is the s3 bucket name to store the artifacts
        * dir is base dir for extracting the tarball, will use system
          tmp dir if None.
    """
    # 1. extract tarball
    tmp_root = _extract_tarball(repo, prod_key, dir__=dir_)

    # 2. scan for paths and filter out the ignored paths,
    # and also collect poms for later metadata generation
    (top_level,
     valid_mvn_paths,
     valid_poms) = _scan_paths(tmp_root, ignore_patterns, root)

    # 3. Parse GA from valid_poms for later maven metadata refreshing
    logger.info("Start generating maven-metadata.xml files for all artifacts")
    logger.debug("Valid poms: %s", valid_poms)
    changed_gavs = parse_gavs(valid_poms, top_level)
    ga_paths = []
    for g, avs in changed_gavs.items():
        for a, _ in avs.items():
            logger.debug("G: %s, A: %s", g, a)
            ga_paths.append(os.path.join("/".join(g.split(".")), a))

    # 4. Delete all valid_paths from s3
    logger.info("Start deleting files from s3")
    s3_client = S3Client()
    bucket = bucket_name if bucket_name else AWS_DEFAULT_BUCKET
    deleted_files, failed_files = s3_client.delete_files(
        valid_mvn_paths,
        bucket_name=bucket,
        product=prod_key,
        root=top_level
    )
    logger.info("Files deletion done\n")

    # 5. Use changed GA to scan s3 for metadata refreshment
    logger.info("Start generating maven-metadata.xml files for all changed GAs")
    meta_files = _generate_metadatas(s3_client, bucket, valid_poms, top_level)
    logger.info("maven-metadata.xml files generation done\n")

    # 6. Upload all maven-metadata.xml. We need to delete metadata files
    # firstly for all affected GA, and then replace the theirs content.
    logger.info("Start updating maven-metadata.xml to s3")
    all_meta_files = []
    for _, files in meta_files.items():
        all_meta_files.extend(files)
    (deleted_metas, _) = s3_client.delete_files(
        file_paths=all_meta_files, bucket_name=bucket, product=prod_key, root=top_level
    )
    deleted_files += deleted_metas
    failed_metas = []
    if META_FILE_GEN_KEY in meta_files:
        _uploaded_files, _failed_metas = s3_client.upload_metadatas(
            meta_file_paths=meta_files[META_FILE_GEN_KEY],
            bucket_name=bucket,
            product=None,
            root=top_level
        )
        failed_metas.extend(_failed_metas)
        for m_file in _uploaded_files:
            if m_file.replace(top_level, '') in deleted_files:
                deleted_files.remove(m_file.replace(top_level, ''))
            elif m_file.replace(top_level + '/', '') in deleted_files:
                deleted_files.remove(m_file.replace(top_level + '/', ''))
    logger.info("maven-metadata.xml updating done\n")

    if do_index:
        logger.info("Start generating index files for all changed entries")
        delete_index, update_index = indexing.handle_delete_index(
            top_level, deleted_files, s3_client, bucket)
        logger.info("Index files generation done.\n")

        logger.info("Start updating index to s3")
        if update_index != []:
            _, _failed_metas = s3_client.upload_metadatas(
                meta_file_paths=update_index,
                bucket_name=bucket,
                product=None,
                root=top_level
            )
            failed_metas.extend(_failed_metas)

        s3_client.delete_files(
            file_paths=delete_index, bucket_name=bucket, product=None, root=top_level
        )
        logger.info("Index files updating done.\n")
    else:
        logger.info("Bypassing indexing")

    if len(failed_files) == 0 and len(failed_metas) == 0:
        logger.info("Product release %s is successfully"
                    " rolled back from Mercury service.", prod_key)
    else:
        logger.warning("Product release %s is rolled back from Mercury"
                       " service, but has some failure as below:",
                       prod_key)
        if len(failed_files) > 0:
            logger.warning("Files failed to delete: \n%s",
                           failed_files)
        if len(failed_metas) > 0:
            logger.warning("Metadata files failed to refresh: \n%s",
                           failed_metas)


def _extract_tarball(repo: str, prefix="", dir__=None) -> str:
    if os.path.exists(repo):
        logger.info("Extracting tarball %s", repo)
        repo_zip = ZipFile(repo)
        tmp_root = mkdtemp(prefix=f"hermes-{prefix}-", dir=dir__)
        extract_zip_all(repo_zip, tmp_root)
        return tmp_root
    logger.error("Error: archive %s does not exist", repo)
    sys.exit(1)


def _scan_paths(files_root: str, ignore_patterns: List[str],
                root: str) -> Tuple[str, List, List]:
    # 2. scan for paths and filter out the ignored paths,
    # and also collect poms for later metadata generation
    logger.info("Scan %s to collect files", files_root)
    top_level = root
    valid_mvn_paths, non_mvn_paths, ignored_paths, valid_poms = [], [], [], []
    top_found = False
    for root_dir, dirs, names in os.walk(files_root):
        for directory in dirs:
            if directory == top_level:
                top_level = os.path.join(root_dir, directory)
                top_found = True
                break
            if os.path.join(root_dir, directory) == os.path.join(files_root, top_level):
                top_level = os.path.join(files_root, top_level)
                top_found = True
                break

        for name in names:
            path = os.path.join(root_dir, name)
            if _is_ignored(name, ignore_patterns):
                ignored_paths.append(name)
                continue
            if top_level in root_dir:
                valid_mvn_paths.append(path)
            else:
                non_mvn_paths.append(path)
            if name.strip().endswith(".pom"):
                logger.debug("Found pom %s", name)
                valid_poms.append(path)

    if len(non_mvn_paths) > 0:
        non_mvn_items = [n.replace(files_root, "") for n in non_mvn_paths]
        logger.info("These files are not under the specified "
                    "prefix dir %s, so will be ignored: \n%s",
                    root, non_mvn_items)
    if not top_found and top_level.strip() != "":
        logger.warning(
            "Warning: the root path %s does not exist in tarball,"
            " will use empty trailing prefix for the uploading",
            top_level
        )
        top_level = files_root
    logger.info("Files scanning done.\n")

    if ignore_patterns and len(ignore_patterns) > 0:
        logger.info(
            "Ignored paths with ignore_patterns %s as below:\n%s\n",
            ignore_patterns, "\n".join(ignored_paths)
        )

    return (top_level, valid_mvn_paths, valid_poms)


def _generate_metadatas(
    s3: S3Client, bucket: str, poms: List[str], root: str
) -> Dict[str, List[str]]:
    """Collect GAVs and generating maven-metadata.xml.
       As all valid poms has been stored in s3 bucket,
       what we should do here is:
       * Scan and get the GA for the poms
       * Search all poms in s3 based on the GA
       * Use searched poms to generate maven-metadata to refresh
    """
    gas_dict: Dict[str, bool] = {}
    logger.debug("Valid poms: %s", poms)
    valid_gavs_dict = parse_gavs(poms, root)
    for g, avs in valid_gavs_dict.items():
        for a in avs.keys():
            logger.debug("G: %s, A: %s", g, a)
            g_path = "/".join(g.split("."))
            gas_dict[os.path.join(g_path, a)] = True
    all_poms = []
    meta_files = {}
    for path, _ in gas_dict.items():
        # avoid some wrong prefix, like searching a/b
        # but got a/b-1
        if not path.endswith("/"):
            path = path + "/"
        existed_poms = s3.get_files(bucket, path, ".pom")
        if len(existed_poms) == 0:
            logger.debug(
                "No poms found in s3 bucket %s for GA path %s", bucket, path
            )
            meta_files_deletion = meta_files.get(META_FILE_DEL_KEY, [])
            meta_files_deletion.append(os.path.join(path, "maven-metadata.xml"))
            meta_files[META_FILE_DEL_KEY] = meta_files_deletion
        else:
            logger.debug(
                "Got poms in s3 bucket %s for GA path %s: %s", bucket, path, poms
            )
            all_poms.extend(existed_poms)
    gav_dict = parse_gavs(all_poms)
    if len(gav_dict) > 0:
        meta_files_generation = []
        for g, avs in gav_dict.items():
            for a, vers in avs.items():
                try:
                    meta_file = gen_meta_file(g, a, vers, root)
                except FileNotFoundError:
                    logger.error("Failed to create metadata file for GA"
                                 " %s, please check if aligned Maven GA"
                                 " is correct in your tarball.", f'{g}:{a}')
                logger.debug("Generated metadata file %s for %s:%s", meta_file, g, a)
                meta_files_generation.append(meta_file)
        meta_files[META_FILE_GEN_KEY] = meta_files_generation
    return meta_files


def _is_ignored(filename: str, ignore_patterns: List[str]) -> bool:
    if ignore_patterns:
        for dirs in ignore_patterns:
            if re.search(dirs, filename):
                return True
    return False


def _validate_maven(paths: List[str]) -> Tuple[List[str], str]:
    # Reminder: need to implement later
    return (list(), True)


def _handle_error(err_msgs: List[str]):
    # Reminder: will implement later
    pass


def ver_cmp_key():
    'Used as key function for version sorting'
    class K:
        def __init__(self, obj):
            self.obj = obj

        def __lt__(self, other):
            return self.__compare(other) < 0

        def __gt__(self, other):
            return self.__compare(other) > 0

        def __eq__(self, other):
            return self.__compare(other) == 0

        def __hash__(self) -> int:
            return self.obj.__hash__()

        def __compare(self, other) -> int:
            xitems = self.obj.split(".")
            if "-" in xitems[-1]:
                xitems = xitems[:-1] + xitems[-1].split("-")
            yitems = other.obj.split(".")
            if "-" in yitems[-1]:
                yitems = yitems[:-1] + yitems[-1].split("-")
            big = max(len(xitems), len(yitems))
            for i in range(big):
                try:
                    xitem = xitems[i]
                except IndexError:
                    return -1
                try:
                    yitem = yitems[i]
                except IndexError:
                    return 1
                if xitem.isnumeric() and yitem.isnumeric():
                    xitem = int(xitem)
                    yitem = int(yitem)
                elif xitem.isnumeric() and not yitem.isnumeric():
                    return 1
                elif not xitem.isnumeric() and yitem.isnumeric():
                    return -1
                if xitem > yitem:
                    return 1
                elif xitem < yitem:
                    return -1
                else:
                    continue
            return 0
    return K