"""
Copyright (C) 2021 Red Hat, Inc. (https://github.com/Commonjava/charon)

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

import argparse
import locale
import logging
import os
import pkg_resources
from typing import List

from charon.cli.internal import set_logging
from charon.constants import PROG, DESCRIPTION
from charon.util import exception_message
from charon.utils.archive import detect_npm_archive, NpmArchiveType
from charon.pkgs.maven import handle_maven_uploading, handle_maven_del
from charon.config import CharonConfig
from json import loads

logger = logging.getLogger("charon")


class CLI(object):
    def __init__(self, formatter_class=argparse.HelpFormatter, prog=PROG):
        self.parser = argparse.ArgumentParser(
            prog=prog,
            description=DESCRIPTION,
            formatter_class=formatter_class,
        )
        self.upload_parser = None
        self.delete_parser = None

        locale.setlocale(locale.LC_ALL, "")

    def set_arguments(self):
        try:
            version = pkg_resources.get_distribution("charon").version
        except pkg_resources.DistributionNotFound:
            version = "GIT"

        exclusive_group = self.parser.add_mutually_exclusive_group()
        exclusive_group.add_argument(
            "-V", "--version", action="version", version=version
        )

        subparsers = self.parser.add_subparsers(help="commands")

        self.__init_upload(subparsers)

        self.__init_delete(subparsers)

    def __init_upload(self, subparsers):
        self.upload_parser = subparsers.add_parser(
            "upload",
            usage=f"{PROG} [OPTIONS] upload",
            description="Upload a product release tarball to charon service",
        )
        self.__init_common(self.upload_parser, charon_upload)

    def __init_delete(self, subparsers):
        self.delete_parser = subparsers.add_parser(
            "delete",
            usage=f"{PROG} [OPTIONS] delete",
            description="Delete a product release tarball from charon service",
        )
        self.__init_common(self.delete_parser, charon_delete)

    def __init_common(self, parser, default_func):
        parser.add_argument("repo", help="The repo tarball localtion in filesystem")
        parser.add_argument(
            "-p",
            "--product",
            help="The product key, used to lookup profileId from the configuration",
            required=True,
        )
        parser.add_argument(
            "-v",
            "--product_version",
            help="The product version, used in repository definition metadata",
            required=True,
        )
        parser.add_argument(
            "-g",
            "--ga",
            default=False,
            help="Push content to the GA group (as opposed to earlyaccess)",
        )
        parser.add_argument(
            "-r",
            "--root_path",
            default="maven-repository",
            help="""The root path in the tarball before the real maven paths,
                    will be trailing off before uploading
            """,
        )
        parser.add_argument(
            "--ignore_patterns",
            "-i",
            default=None,
            action='append',
            nargs=1,
            help="""The regex patterns list to filter out the paths which should
                    not be allowed to upload to S3. )
            """,
        )

        exclusive_group = parser.add_mutually_exclusive_group()
        exclusive_group.add_argument("-q", "--quiet", action="store_true")
        exclusive_group.add_argument("-d", "--debug", action="store_true")

        parser.set_defaults(func=default_func)

    def run(self):
        self.set_arguments()
        args = self.parser.parse_args()
        logging.captureWarnings(True)

        try:
            if args.debug:
                set_logging(level=logging.DEBUG)
            elif args.quiet:
                set_logging(level=logging.WARNING)
            else:
                set_logging(level=logging.INFO)
            args.func(args)
        except AttributeError:
            if hasattr(args, "func"):
                raise
            else:
                self.parser.print_help()
        except KeyboardInterrupt:
            pass
        except Exception as ex:
            if args.debug:
                raise
            else:
                logger.error("exception caught: %s", exception_message(ex))


def charon_upload(args):
    npm_archive_type = detect_npm_archive(args.repo)
    product_key = f"{args.product}-{args.product_version}"
    if npm_archive_type != NpmArchiveType.NOT_NPM:
        # if any npm archive types....
        # Reminder: do npm repo handling here
        logger.info("This is a npm archive")
    else:
        logger.info("This is a maven archive")
        ignore_patterns_list = None
        if args.ignore_patterns:
            ignore_patterns_list = args.ignore_patterns
        else:
            ignore_patterns_list = __get_ignore_patterns()
        handle_maven_uploading(
            args.repo, product_key, args.ga, ignore_patterns_list, root=args.root_path
        )


def charon_delete(args):
    npm_archive_type = detect_npm_archive(args.repo)
    product_key = f"{args.product}-{args.product_version}"
    if npm_archive_type != NpmArchiveType.NOT_NPM:
        # if any npm archive types....
        # Reminder: do npm repo handling here
        logger.info("This is a npm archive")
    else:
        logger.info("This is a maven archive")
        ignore_patterns_list = None
        if args.ignore_patterns:
            ignore_patterns_list = args.ignore_patterns
        else:
            ignore_patterns_list = __get_ignore_patterns()
        handle_maven_del(
            args.repo, product_key, args.ga, ignore_patterns_list, root=args.root_path
        )


def __get_ignore_patterns() -> List[str]:
    ignore_patterns = os.getenv("charon_IGNORE_PATTERNS")
    if ignore_patterns:
        try:
            return loads(ignore_patterns)
        except (ValueError, TypeError):
            logger.warning("Warning: ignore_patterns %s specified in "
                           "system environment, but not a valid json "
                           "style array. Will skip it.", ignore_patterns)
    conf = CharonConfig()
    if conf:
        return conf.get_ignore_patterns()
    return None


def run():
    cli = CLI()
    cli.run()


if __name__ == "__main__":
    run()
