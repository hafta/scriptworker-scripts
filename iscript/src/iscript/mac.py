#!/usr/bin/env python
"""iscript mac signing/notarization functions."""
import arrow
import asyncio
import attr
from glob import glob
import logging
import os
import pexpect
import re

from scriptworker_client.utils import (
    extract_tarball,
    get_artifact_path,
    list_files,
    makedirs,
    rm,
    run_command,
)
from iscript.utils import (
    create_zipfile,
    raise_future_exceptions,
    semaphore_wrapper,
)
from iscript.exceptions import (
    InvalidNotarization,
    IScriptError,
    TimeoutError,
    UnknownAppDir,
)

log = logging.getLogger(__name__)


INITIAL_FILES_TO_SIGN = (
    'Contents/MacOS/XUL',
    'Contents/MacOS/pingsender',
    'Contents/MacOS/*.dylib',
    'Contents/MacOS/crashreporter.app/Contents/MacOS/minidump-analyzer',
    'Contents/MacOS/crashreporter.app/Contents/MacOS/crashreporter',
    'Contents/MacOS/firefox-bin',
    'Contents/MacOS/plugin-container.app/Contents/MacOS/plugin-container',
    'Contents/MacOS/updater.app/Contents/MacOS/org.mozilla.updater',
    'Contents/MacOS/firefox',
)


@attr.s
class App(object):
    orig_path = attr.ib(default='')
    parent_dir = attr.ib(default='')
    app_path = attr.ib(default='')
    app_name = attr.ib(default='')
    zip_path = attr.ib(default='')
    notarization_log_path = attr.ib(default='')
    target_path = attr.ib(default='')

    def check_required_attrs(self, required_attrs):
        """Make sure the ``required_attrs`` are set.

        Args:
            required_attrs (list): list of attribute strings

        Raises:
            IScriptError: on missing attr

        """
        for att in required_attrs:
            if not hasattr(self, att) or not getattr(self, att):
                raise IScriptError('Missing {} attr!'.format(att))


# sign {{{1
async def sign(config, app, key, entitlements_path):
    """Sign the .app.

    Args:
        config (dict): the running config
        from_ (str): the tarfile path
        parent_dir (str): the top level directory to extract the app into
        key (str): the nick of the key to use to sign with

    Raises:
        IScriptError: on error.

    """
    key_config = get_key_config(config, key)
    app.app_path = get_app_dir(app.parent_dir)
    app.app_name = os.path.basename(app.app_path)
    await run_command(
        ['xattr', '-cr', app.app_name], cwd=app.parent_dir,
        exception=IScriptError
    )
    # find initial files from INITIAL_FILES_TO_SIGN globs
    initial_files = []
    for path in INITIAL_FILES_TO_SIGN:
        initial_files.extend(glob(os.path.join(app.app_path, path)))

    # sign initial files
    futures = []
    semaphore = asyncio.Semaphore(10)
    for path in initial_files:
        futures.append(asyncio.ensure_future(
            semaphore_wrapper(
                semaphore,
                run_command,
                [
                    'codesign', '--force', '-o', 'runtime', '--verbose',
                    '--sign', key_config['identity'], '--entitlements',
                    entitlements_path, path
                ],
                cwd=app.parent_dir, exception=IScriptError
            )
        ))
    await raise_future_exceptions(futures)

    # sign everything
    futures = []
    for path in list_files(app.app_path):
        if path in initial_files:
            continue
        futures.append(
            semaphore_wrapper(
                semaphore,
                run_command,
                [
                    'codesign', '--force', '-o', 'runtime', '--verbose',
                    '--sign', key_config['identity'], '--entitlements',
                    entitlements_path, path
                ],
                cwd=app.parent_dir, exception=IScriptError
            )
        )
    await raise_future_exceptions(futures)

    # sign bundle
    await run_command(
        [
            'codesign', '--force', '-o', 'runtime', '--verbose',
            '--sign', key_config['identity'], '--entitlements',
            entitlements_path, app.app_name
        ],
        cwd=app.parent_dir, exception=IScriptError
    )

    # verify bundle
    await run_command(
        [
            'codesign', '-vvv', '--deep', '--strict', app.app_name
        ],
        cwd=app.parent_dir, exception=IScriptError
    )


# unlock_keychain {{{1
async def unlock_keychain(signing_keychain, keychain_password):
    """Unlock the signing keychain.

    Args:
        signing_keychain (str): the path to the signing keychain
        keychain_password (str): the keychain password

    Raises:
        IScriptError: on failure
        TimeoutFailure: on timeout

    """
    log.info("Unlocking signing keychain {}".format(signing_keychain))
    child = pexpect.spawn('security', ['unlock-keychain', signing_keychain], encoding='utf-8')
    try:
        while True:
            index = child.expect([pexpect.EOF, r"password to unlock.*: "], async_=True)
            if index == 0:
                break
            child.sendline(b'keychain_password')
    except (pexpect.exceptions.TIMEOUT) as exc:
        raise TimeoutError("Timeout trying to unlock the keychain {}: {}!".format(signing_keychain, exc))
    child.close()
    if child.exitstatus != 0 or child.signalstatus is not None:
        raise IScriptError(
            "Failed unlocking {}! exit {} signal {}".format(
                signing_keychain, child.exitstatus, child.signalstatus
            )
        )


# get_app_dir {{{1
def get_app_dir(parent_dir):
    """Get the .app directory in a ``parent_dir``.

    This assumes there is one, and only one, .app directory in ``parent_dir``.

    Args:
        parent_dir (str): the parent directory path

    Raises:
        UnknownAppDir: if there is no single app dir

    """
    apps = glob('{}/*.app'.format(parent_dir))
    if len(apps) != 1:
        raise UnknownAppDir("Can't find a single .app in {}: {}".format(
            parent_dir, apps
        ))
    return apps[0]


# get_key_config {{{1
def get_key_config(config, key, config_key='mac_config'):
    """Get the key subconfig from ``config``.

    Args:
        config (dict): the running config
        key (str): the key nickname, e.g. ``dep``
        config_key (str): the config key to use, e.g. ``mac_config``

    Raises:
        IScriptError: on invalid ``key`` or ``config_key``

    Returns:
        dict: the subconfig for the given ``config_key`` and ``key``

    """
    try:
        return config[config_key][key]
    except KeyError as e:
        raise IScriptError('Unknown key config {} {}: {}'.format(config_key, key, e))


# get_app_paths {{{1
def get_app_paths(config, task):
    """Create a list of ``App`` objects from the task.

    These will have their ``orig_path`` set.

    Args:
        config (dict): the running config
        task (dict): the running task

    Returns:
        list: a list of App objects

    """
    all_paths = []
    for upstream_artifact_info in task['payload']['upstreamArtifacts']:
        for subpath in upstream_artifact_info['paths']:
            orig_path = get_artifact_path(
                upstream_artifact_info['taskId'], subpath, work_dir=config['work_dir'],
            )
            all_paths.append(App(orig_path=orig_path))
    return all_paths


# extract_all {{{1
async def extract_all_apps(work_dir, all_paths):
    """Extract all the apps into their own directories.

    Args:
        work_dir (str): the ``work_dir`` path
        all_paths (list): a list of ``App`` objects with their ``orig_path`` set

    Raises:
        IScriptError: on failure

    """
    log.info("Extracting all apps")
    futures = []
    for counter, app in enumerate(all_paths):
        app.check_required_attrs(['orig_path'])
        app.parent_dir = os.path.join(work_dir, str(counter))
        rm(app.parent_dir)
        makedirs(app.parent_dir)
        futures.append(asyncio.ensure_future(
            extract_tarball(app.orig_path, app.parent_dir)
        ))
    await raise_future_exceptions(futures)


# create_all_app_zipfiles {{{1
async def create_all_app_zipfiles(all_paths):
    """Create notarization zipfiles for all the apps.

    Args:
        all_paths (list): list of ``App`` objects

    Raises:
        IScriptError: on failure

    """
    futures = []
    required_attrs = ['parent_dir', 'zip_path', 'app_path']
    # zip up apps
    for app in all_paths:
        app.check_required_attrs(required_attrs)
        app.zip_path = os.path.join(
            app.parent_dir, "{}.zip".format(os.path.basename(app.parent_dir))
        )
        # ditto -c -k --norsrc --keepParent "${BUNDLE}" ${OUTPUT_ZIP_FILE}
        futures.append(asyncio.ensure_future(
            create_zipfile(
                app.zip_path, app.app_path, app.parent_dir,
            )
        ))
    await raise_future_exceptions(futures)


# sign_all_apps {{{1
async def sign_all_apps(key_config, entitlements_path, all_paths):
    """Sign all the apps.

    Args:
        key_config (dict): the config for this signing key
        entitlements_path (str): the path to the entitlements file, used
            for signing
        all_paths (list): the list of ``App`` objects

    Raises:
        IScriptError: on failure

    """
    log.info("Signing all apps")
    futures = []
    for app in all_paths:
        futures.append(asyncio.ensure_future(
            sign(key_config, app, entitlements_path)
        ))
    await raise_future_exceptions(futures)


# get_bundle_id {{{1
def get_bundle_id(base_bundle_id):
    """Get a bundle id for notarization

    Args:
        base_bundle_id (str): the base string to use for the bundle id

    Returns:
        str: the bundle id

    """
    now = arrow.utcnow()
    # XXX we may want to encode more information in here. runId?
    return "{}.{}.{}".format(
        base_bundle_id,
        os.environ.get('TASK_ID', 'None'),
        "{}{}".format(now.timestamp, now.microsecond),
    )


# get_uuid_from_log {{{1
def get_uuid_from_log(log_path):
    """Get the UUID from the notarization log.

    Args:
        log_path (str): the path to the log

    Raises:
        IScriptError: if we can't find the UUID

    Returns:
        str: the uuid

    """
    try:
        with open(log_path, 'r') as fh:
            for line in fh.readline():
                # XXX double check this looks like a uuid? Perhaps switch to regex
                if line.startswith('RequestUUID ='):
                    parts = line.split(' ')
                    return parts[2]
    except OSError as err:
        raise IScriptError("Can't find UUID in {}: {}".format(log_path, err))
    raise IScriptError("Can't find UUID in {}!".format(log_path))


# get_notarization_status_from_log {{{1
def get_notarization_status_from_log(log_path):
    """Get the status from the notarization log.

    Args:
        log_path (str): the path to the log file to parse

    Returns:
        str: either ``success`` or ``invalid``, depending on status
        None: if we have neither success nor invalid status

    """
    regex = re.compile(r'Status: (?P<status>success|invalid)')
    try:
        with open(log_path, 'r') as fh:
            contents = fh.read()
        m = regex.search(contents)
        if m is not None:
            return m.status
    except OSError:
        return


# wrap_notarization_with_sudo {{{1
async def wrap_notarization_with_sudo(config, key_config, all_paths):
    """Wrap the notarization requests with sudo.

    Apple creates a lockfile per user for notarization. To notarize concurrently,
    we use sudo against a set of accounts (``config['local_notarization_accounts']``).

    Raises:
        IScriptError: on failure

    Returns:
        dict: uuid to log path

    """
    futures = []
    accounts = config['local_notarization_accounts']
    counter = 0
    uuids = {}

    for app in all_paths:
        app.check_required_attrs(['zip_path'])

    while counter < len(all_paths):
        futures = []
        for account in accounts:
            app = all_paths[counter]
            app.notarization_log_path = os.path.join(app.parent_dir, 'notarization.log')
            bundle_id = get_bundle_id(key_config['base_bundle_id'])
            base_cmd = [
                'sudo', '-u', account,
                'xcrun', 'altool', '--notarize-app',
                '-f', app.zip_path,
                '--primary-bundle-id', bundle_id,
                '-u', key_config['apple_notarization_account'],
                '--password',
            ]
            log_cmd = base_cmd + ['********']
            # TODO wrap in retry?
            futures.append(asyncio.ensure_future(
                run_command(
                    base_cmd + [key_config['apple_notarization_password']],
                    log_path=app.notarization_log_path,
                    log_cmd=log_cmd,
                    exception=IScriptError,
                )
            ))
            counter += 1
            if counter >= len(all_paths):
                break
        await raise_future_exceptions(futures)
    for app in all_paths:
        uuids[get_uuid_from_log(app.notarization_log_path)] = app.notarization_log_path
    return uuids


# poll_notarization_uuid {{{1
async def poll_notarization_uuid(uuid, username, password, timeout, log_path, sleep_time=15):
    """Poll to see if the notarization for ``uuid`` is complete.

    Args:
        uuid (str): the uuid to poll for
        username (str): the apple user to poll with
        password (str): the apple password to poll with
        timeout (int): the maximum wait time
        sleep_time (int): the time to sleep between polling

    Raises:
        TimeoutError: on timeout
        InvalidNotarization: if the notarization fails with ``invalid``
        IScriptError: on unexpected failure

    """
    start = arrow.utcnow().timestamp
    timeout_time = start + timeout
    base_cmd = ['xcrun', 'altool', '--notarization-info', uuid, '-u', username, '--password']
    log_cmd = base_cmd + ['********']
    while 1:
        await run_command(
            base_cmd + [password], log_path=log_path, log_cmd=log_cmd,
            exception=IScriptError,
        )
        status = get_notarization_status_from_log(log_path)
        if status == 'success':
            break
        if status == 'invalid':
            raise InvalidNotarization('Invalid notarization for uuid {}!'.format(uuid))
        await asyncio.sleep(sleep_time)
        if arrow.utcnow().timestamp > timeout_time:
            raise TimeoutError("Timed out polling for uuid {}!".format(uuid))


# sign_and_notarize_all {{{1
async def sign_and_notarize_all(config, task):
    """Sign and notarize all mac apps for this task.

    Args:
        config (dict): the running configuration
        task (dict): the running task

    Raises:
        IScriptError: on fatal error.

    """
    work_dir = config['work_dir']
    # TODO get entitlements -- default or from url
    entitlements_path = os.path.join(work_dir, "browser.entitlements.txt")

    # TODO get this from scopes?
    key = 'dep'
    key_config = get_key_config(config, key)

    all_paths = get_app_paths(config, task)
    await extract_all_apps(work_dir, all_paths)
    await unlock_keychain(key_config['signing_keychain'], key_config['keychain_password'])
    await sign_all_apps(key_config, entitlements_path, all_paths)

    log.info("Notarizing")
    if key_config['notarize_type'] == 'multi_account':
        await create_all_app_zipfiles(all_paths)
        poll_uuids = await wrap_notarization_with_sudo(config, key_config, all_paths)
    # TODO else create a zip for all apps, poll without sudo

    log.info("Polling for notarization status")
    futures = []
    for uuid, log_path in poll_uuids.items():
        futures.append(asyncio.ensure_future(
            poll_notarization_uuid(
                uuid, key_config['apple_notarization_account'],
                key_config['apple_notarization_password'],
                key_config['notarization_poll_timeout'],
                log_path, sleep_time=15
            )
        ))
    results = await raise_future_exceptions(futures)
    if set(results) != {'success'}:
        raise IScriptError("Failure polling notarization!")  # XXX This may not be reachable

    log.info("Stapling apps")
    for app in all_paths:
        # XXX do this concurrently if it saves us time without breaking
        await run_command(
            ['xcrun', 'stapler', 'staple', '-v', app.app_name],
            cwd=app.parent_dir, exception=IScriptError
        )

    log.info("Tarring up artifacts")
    futures = []
    for app in all_paths:
        # If we downloaded public/build/locale/target.tar.gz, then write to
        # artifact_dir/public/build/locale/target.tar.gz
        app.target_path = '{}/public/{}'.format(
            config['artifact_dir'], app.orig_path.split('public/')
        )
        os.makedirs(os.path.dirname(app.target_path))
        # TODO: different tar commands based on suffix?
        futures.append(run_command(
            ['tar', 'czvf', app.target_path, app.app_name],
            cwd=app.parent_dir, exception=IScriptError,
        ))
    await raise_future_exceptions(futures)

    log.info("Creating PKG files")
    futures = []
    for app in all_paths:
        pkg_path = app.target_path.replace('tar.gz', 'pkg')
        futures.append(run_command(
            [
                'sudo', 'pkgbuild', '--install-location', '/Applications', '--component',
                app.app_path, pkg_path
            ],
            cwd=app.parent_dir, exception=IScriptError
        ))
    await raise_future_exceptions(futures)

    # TODO sign pkg? If so, we might write to a tmp location, then sign and
    # copy to the artifact_dir

    log.info("Done signing and notarizing apps.")
