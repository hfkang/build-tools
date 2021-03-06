#!/usr/bin/env python

import site
import time
import logging
import sys
import os
import re
import subprocess
import hashlib
import functools
import shutil
import tempfile
import requests
from os import path
from optparse import OptionParser
from twisted.python.lockfile import FilesystemLock

site.addsitedir(path.join(path.dirname(__file__), "../../lib/python"))

from kickoff import get_partials, ReleaseRunner, make_task_graph_strict_kwargs
from kickoff import get_l10n_config, get_en_US_config
from kickoff import email_release_drivers
from kickoff import bump_version
from kickoff.sanity import ReleaseSanitizerRunner, SanityException, is_candidate_release
from release.info import readBranchConfig
from release.l10n import parsePlainL10nChangesets
from release.versions import getAppVersion
from taskcluster import Scheduler, Index, Queue
from taskcluster.utils import slugId
from util.hg import mercurial
from util.retry import retry
from util.file import load_config, get_config

log = logging.getLogger(__name__)


# both CHECKSUMS and ALL_FILES have been defined to improve the release sanity
# en-US binaries timing by whitelisting artifacts of interest - bug 1251761
CHECKSUMS = set([
    '.checksums',
    '.checksums.asc',
])


ALL_FILES = set([
    '.checksums',
    '.checksums.asc',
    '.complete.mar',
    '.exe',
    '.dmg',
    'i686.tar.bz2',
    'x86_64.tar.bz2',
])


def update_channels(version, mappings):
    """Return a list of update channels for a version using version mapping

    >>> update_channels("40.0", [(r"^\d+\.0$", ["beta", "release"]), (r"^\d+\.\d+\.\d+$", ["release"])])
    ["beta", "release"]
    >>> update_channels("40.0.1", [(r"^\d+\.0$", ["beta", "release"]), (r"^\d+\.\d+\.\d+$", ["release"])])
    ["release"]

    """
    for pattern, channels in mappings:
        if re.match(pattern, version):
            return channels
    raise RuntimeError("Cannot find update channels for %s" % version)


def validate_signatures(checksums, signature, dir_path, gpg_key_path):
    try:
        cmd = ['gpg', '--batch', '--homedir', dir_path, '--import',
               gpg_key_path]
        subprocess.check_call(cmd)
        cmd = ['gpg', '--homedir', dir_path, '--verify', signature, checksums]
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError:
        log.exception("GPG signature check failed")
        raise SanityException("GPG signature check failed")


def parse_sha512(checksums, files):
    # parse the checksums file and store all sha512 digests
    _dict = dict()
    with open(checksums, 'rb') as fd:
        lines = fd.readlines()
        for line in lines:
            digest, alg, _, name = line.split()
            if alg != 'sha512':
                continue
            _dict[os.path.basename(name)] = digest
    wdict = {k: _dict[k] for k in _dict.keys() if file_in_whitelist(k, files)}
    return wdict


def download_all_artifacts(queue, artifacts, task_id, dir_path):
    failed_downloads = False

    for artifact in artifacts:
        name = os.path.basename(artifact)
        build_url = queue.buildSignedUrl(
            'getLatestArtifact',
            task_id,
            artifact
        )
        log.debug('Downloading %s', name)
        try:
            r = requests.get(build_url, timeout=60)
            r.raise_for_status()
        except requests.HTTPError:
            log.exception("Failed to download %s", name)
            failed_downloads = True
        else:
            filepath = os.path.join(dir_path, name)
            with open(filepath, 'wb') as fd:
                for chunk in r.iter_content(1024):
                    fd.write(chunk)

    if failed_downloads:
        raise SanityException('Downloading artifacts failed')


def validate_checksums(_dict, dir_path):
    for name in _dict.keys():
        filepath = os.path.join(dir_path, name)
        computed_hash = get_hash(filepath)
        correct_hash = _dict[name]
        if computed_hash != correct_hash:
            log.error("failed to validate checksum for %s", name, exc_info=True)
            raise SanityException("Failed to check digest for %s" % name)


def file_in_whitelist(artifact, whitelist):
    return any([artifact.endswith(x) for x in whitelist])


def sanitize_en_US_binary(queue, task_id, gpg_key_path):
    # each platform en-US gets its own tempdir workground
    tempdir = tempfile.mkdtemp()
    log.debug('Temporary playground is %s', tempdir)

    # get all artifacts and trim but 'name' field from the json entries
    all_artifacts = [k['name'] for k in queue.listLatestArtifacts(task_id)['artifacts']]
    # filter files to hold the whitelist-related only
    artifacts = filter(lambda k: file_in_whitelist(k, ALL_FILES), all_artifacts)
    # filter out everything but the checkums artifacts
    checksums_artifacts = filter(lambda k: file_in_whitelist(k, CHECKSUMS), all_artifacts)
    other_artifacts = list(set(artifacts) - set(checksums_artifacts))
    # iterate in artifacts and grab checksums and its signature only
    log.info("Retrieve the checksums file and its signature ...")
    for artifact in checksums_artifacts:
        name = os.path.basename(artifact)
        build_url = queue.buildSignedUrl(
            'getLatestArtifact',
            task_id,
            artifact
        )
        log.debug('Downloading %s', name)
        try:
            r = requests.get(build_url, timeout=60)
            r.raise_for_status()
        except requests.HTTPError:
            log.exception("Failed to download %s file", name)
            raise SanityException("Failed to download %s file" % name)
        filepath = os.path.join(tempdir, name)
        with open(filepath, 'wb') as fd:
            for chunk in r.iter_content(1024):
                fd.write(chunk)
        if name.endswith(".checksums.asc"):
            signature = filepath
        else:
            checksums = filepath

    # perform the signatures validation test
    log.info("Attempt to validate signatures ...")
    validate_signatures(checksums, signature, tempdir, gpg_key_path)
    log.info("Signatures validated correctly!")

    log.info("Download all artifacts ...")
    download_all_artifacts(queue, other_artifacts, task_id, tempdir)
    log.info("All downloads completed!")

    log.info("Retrieve all sha512 from checksums file...")
    sha512_dict = parse_sha512(checksums, ALL_FILES - CHECKSUMS)
    log.info("All sha512 digests retrieved")

    log.info("Validating checksums for each artifact ...")
    validate_checksums(sha512_dict, tempdir)
    log.info("All checksums validated!")

    # remove entire playground before moving forward
    log.debug("Deleting the temporary playground ...")
    shutil.rmtree(tempdir)


def get_hash(path, hash_type="sha512"):
    h = hashlib.new(hash_type)
    with open(path, "rb") as f:
        for chunk in iter(functools.partial(f.read, 4096), ''):
            h.update(chunk)
    return h.hexdigest()


def validate_graph_kwargs(queue, gpg_key_path, **kwargs):
    # TODO: to be moved under kickoff soon, once new relpro sanity is in place
    # bug 1282959
    platforms = kwargs.get('en_US_config', {}).get('platforms', {})
    for platform in platforms.keys():
        task_id = platforms.get(platform).get('task_id', {})
        log.info('Performing release sanity for %s en-US binary', platform)
        sanitize_en_US_binary(queue, task_id, gpg_key_path)

    log.info("Release sanity for all en-US is now completed!")

    log.info("Sanitizing the rest of the release ...")
    sanitizer = ReleaseSanitizerRunner(**kwargs)
    sanitizer.run()
    if not sanitizer.was_successful():
        errors = sanitizer.get_errors()
        raise SanityException("Issues on release sanity %s" % errors)


def main(options):
    log.info('Loading config from %s' % options.config)
    config = load_config(options.config)

    if config.getboolean('release-runner', 'verbose'):
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO
    logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s",
                        level=log_level)
    # Suppress logging of retry(), see bug 925321 for the details
    logging.getLogger("util.retry").setLevel(logging.WARN)

    # Shorthand
    api_root = config.get('api', 'api_root')
    username = config.get('api', 'username')
    password = config.get('api', 'password')
    buildbot_configs = config.get('release-runner', 'buildbot_configs')
    buildbot_configs_branch = config.get('release-runner',
                                         'buildbot_configs_branch')
    sleeptime = config.getint('release-runner', 'sleeptime')
    notify_from = get_config(config, 'release-runner', 'notify_from', None)
    notify_to = get_config(config, 'release-runner', 'notify_to', None)
    docker_worker_key = get_config(config, 'release-runner',
                                   'docker_worker_key', None)
    signing_pvt_key = get_config(config, 'signing', 'pvt_key', None)
    if isinstance(notify_to, basestring):
        notify_to = [x.strip() for x in notify_to.split(',')]
    smtp_server = get_config(config, 'release-runner', 'smtp_server',
                             'localhost')
    tc_config = {
        "credentials": {
            "clientId": get_config(config, "taskcluster", "client_id", None),
            "accessToken": get_config(config, "taskcluster", "access_token", None),
        }
    }
    configs_workdir = 'buildbot-configs'
    balrog_username = get_config(config, "balrog", "username", None)
    balrog_password = get_config(config, "balrog", "password", None)
    extra_balrog_submitter_params = get_config(config, "balrog", "extra_balrog_submitter_params", None)
    beetmover_aws_access_key_id = get_config(config, "beetmover", "aws_access_key_id", None)
    beetmover_aws_secret_access_key = get_config(config, "beetmover", "aws_secret_access_key", None)
    gpg_key_path = get_config(config, "signing", "gpg_key_path", None)

    # TODO: replace release sanity with direct checks of en-US and l10n revisions (and other things if needed)

    rr = ReleaseRunner(api_root=api_root, username=username, password=password)
    scheduler = Scheduler(tc_config)
    index = Index(tc_config)
    queue = Queue(tc_config)

    # Main loop waits for new releases, processes them and exits.
    while True:
        try:
            log.debug('Fetching release requests')
            rr.get_release_requests()
            if rr.new_releases:
                for release in rr.new_releases:
                    log.info('Got a new release request: %s' % release)
                break
            else:
                log.debug('Sleeping for %d seconds before polling again' %
                          sleeptime)
                time.sleep(sleeptime)
        except:
            log.error("Caught exception when polling:", exc_info=True)
            sys.exit(5)

    retry(mercurial, args=(buildbot_configs, configs_workdir), kwargs=dict(branch=buildbot_configs_branch))

    if 'symlinks' in config.sections():
        format_dict = dict(buildbot_configs=configs_workdir)
        for target in config.options('symlinks'):
            symlink = config.get('symlinks', target).format(**format_dict)
            if path.exists(symlink):
                log.warning("Skipping %s -> %s symlink" % (symlink, target))
            else:
                log.info("Adding %s -> %s symlink" % (symlink, target))
                os.symlink(target, symlink)

    # TODO: this won't work for Thunderbird...do we care?
    branch = release["branch"].split("/")[-1]
    branchConfig = readBranchConfig(path.join(configs_workdir, "mozilla"), branch=branch)

    release_channels = update_channels(release["version"], branchConfig["release_channel_mappings"])
    # candidate releases are split in two graphs and release-runner only handles the first
    # graph of tasks. so parts like postrelease, push_to_releases/mirrors, and mirror dependant
    # channels are handled in the second generated graph outside of release-runner.
    # This is not elegant but it should do the job for now
    candidate_release = is_candidate_release(release_channels)
    if candidate_release:
        postrelease_enabled = False
        postrelease_bouncer_aliases_enabled = False
        final_verify_channels = [
            c for c in release_channels if c not in branchConfig.get('mirror_requiring_channels', [])
        ]
        publish_to_balrog_channels = [
            c for c in release_channels if c not in branchConfig.get('mirror_requiring_channels', [])
        ]
        push_to_releases_enabled = False
    else:
        postrelease_enabled = branchConfig['postrelease_version_bump_enabled']
        postrelease_bouncer_aliases_enabled = branchConfig['postrelease_bouncer_aliases_enabled']
        final_verify_channels = release_channels
        publish_to_balrog_channels = release_channels
        push_to_releases_enabled = True

    rc = 0
    for release in rr.new_releases:
        graph_id = slugId()
        try:
            rr.update_status(release, 'Generating task graph')
            l10n_changesets = parsePlainL10nChangesets(rr.get_release_l10n(release["name"]))

            kwargs = {
                "public_key": docker_worker_key,
                "version": release["version"],
                # ESR should not use "esr" suffix here:
                "next_version": bump_version(release["version"].replace("esr", "")),
                "appVersion": getAppVersion(release["version"]),
                "buildNumber": release["buildNumber"],
                "source_enabled": True,
                "checksums_enabled": True,
                "repo_path": release["branch"],
                "revision": release["mozillaRevision"],
                "product": release["product"],
                # if mozharness_revision is not passed, use 'revision'
                "mozharness_changeset": release.get('mh_changeset') or release['mozillaRevision'],
                "partial_updates": get_partials(rr, release['partials'], release['product']),
                "branch": branch,
                "updates_enabled": bool(release["partials"]),
                "l10n_config": get_l10n_config(
                    index=index, product=release["product"], branch=branch,
                    revision=release['mozillaRevision'],
                    platforms=branchConfig['platforms'],
                    l10n_platforms=branchConfig['l10n_release_platforms'],
                    l10n_changesets=l10n_changesets
                ),
                "en_US_config": get_en_US_config(
                    index=index, product=release["product"], branch=branch,
                    revision=release['mozillaRevision'],
                    platforms=branchConfig['release_platforms']
                ),
                "dashboard_check": release['dashboardCheck'],
                "verifyConfigs": {},
                "balrog_api_root": branchConfig["balrog_api_root"],
                "funsize_balrog_api_root": branchConfig["funsize_balrog_api_root"],
                "balrog_username": balrog_username,
                "balrog_password": balrog_password,
                "beetmover_aws_access_key_id": beetmover_aws_access_key_id,
                "beetmover_aws_secret_access_key": beetmover_aws_secret_access_key,
                # TODO: stagin specific, make them configurable
                "signing_class": "release-signing",
                "bouncer_enabled": branchConfig["bouncer_enabled"],
                "updates_builder_enabled": branchConfig["updates_builder_enabled"],
                "update_verify_enabled": branchConfig["update_verify_enabled"],
                "release_channels": release_channels,
                "final_verify_channels": final_verify_channels,
                "final_verify_platforms": branchConfig['release_platforms'],
                "uptake_monitoring_platforms": branchConfig['release_platforms'],
                "signing_pvt_key": signing_pvt_key,
                "build_tools_repo_path": branchConfig['build_tools_repo_path'],
                "push_to_candidates_enabled": branchConfig['push_to_candidates_enabled'],
                "postrelease_bouncer_aliases_enabled": postrelease_bouncer_aliases_enabled,
                "uptake_monitoring_enabled": branchConfig['uptake_monitoring_enabled'],
                "tuxedo_server_url": branchConfig['tuxedoServerUrl'],
                "postrelease_version_bump_enabled": postrelease_enabled,
                "push_to_releases_enabled": push_to_releases_enabled,
                "push_to_releases_automatic": branchConfig['push_to_releases_automatic'],
                "beetmover_candidates_bucket": branchConfig["beetmover_buckets"][release["product"]],
                "partner_repacks_platforms": branchConfig.get("partner_repacks_platforms", []),
                "l10n_changesets": l10n_changesets,
                "extra_balrog_submitter_params": extra_balrog_submitter_params,
                "publish_to_balrog_channels": publish_to_balrog_channels,
            }

            validate_graph_kwargs(queue, gpg_key_path, **kwargs)
            graph = make_task_graph_strict_kwargs(**kwargs)
            rr.update_status(release, "Submitting task graph")
            log.info("Task graph generated!")
            import pprint
            log.debug(pprint.pformat(graph, indent=4, width=160))
            print scheduler.createTaskGraph(graph_id, graph)

            rr.mark_as_completed(release)
            email_release_drivers(smtp_server=smtp_server, from_=notify_from,
                                  to=notify_to, release=release,
                                  graph_id=graph_id)
        except:
            # We explicitly do not raise an error here because there's no
            # reason not to start other releases if creating the Task Graph
            # fails for another one. We _do_ need to set this in order to exit
            # with the right code, though.
            rc = 2
            rr.mark_as_failed(
                release,
                'Failed to start release promotion (graph ID: %s)' % graph_id)
            log.exception("Failed to start release promotion for graph %s %s",
                          graph_id, release)

    if rc != 0:
        sys.exit(rc)

if __name__ == '__main__':
    parser = OptionParser(__doc__)
    parser.add_option('-l', '--lockfile', dest='lockfile',
                      default=path.join(os.getcwd(), ".release-runner.lock"))
    parser.add_option('-c', '--config', dest='config',
                      help='Configuration file')

    options = parser.parse_args()[0]

    if not options.config:
        parser.error('Need to pass a config')

    lockfile = options.lockfile
    log.debug("Using lock file %s", lockfile)
    lock = FilesystemLock(lockfile)
    if not lock.lock():
        raise Exception("Cannot acquire lock: %s" % lockfile)
    log.debug("Lock acquired: %s", lockfile)
    if not lock.clean:
        log.warning("Previous run did not properly exit")
    try:
        main(options)
    finally:
        log.debug("Releasing lock: %s", lockfile)
        lock.unlock()
