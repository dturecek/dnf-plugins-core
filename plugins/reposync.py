# reposync.py
# DNF plugin adding a command to download all packages from given remote repo.
#
# Copyright (C) 2014 Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#

from __future__ import absolute_import
from __future__ import unicode_literals

import os
import shutil

from dnfpluginscore import _, logger
from dnf.cli.option_parser import OptionParser
import dnf
import dnf.cli


def _pkgdir(intermediate, target):
    cwd = dnf.i18n.ucd(os.getcwd())
    return os.path.normpath(os.path.join(cwd, intermediate, target))


class RPMPayloadLocation(dnf.repo.RPMPayload):
    def __init__(self, pkg, progress, pkg_location):
        super(RPMPayloadLocation, self).__init__(pkg, progress)
        self.package_dir = os.path.dirname(pkg_location)

    def _target_params(self):
        tp = super(RPMPayloadLocation, self)._target_params()
        dnf.util.ensure_dir(self.package_dir)
        tp['dest'] = self.package_dir
        return tp

@dnf.plugin.register_command
class RepoSyncCommand(dnf.cli.Command):
    aliases = ('reposync',)
    summary = _('download all packages from remote repo')

    def __init__(self, cli):
        super(RepoSyncCommand, self).__init__(cli)
        self._repo_target = dict()

    @staticmethod
    def set_argparser(parser):
        parser.add_argument('-a', '--arch', dest='arches', default=[],
                            action=OptionParser._SplitCallback, metavar='[arch]',
                            help=_('download only packages for this ARCH'))
        parser.add_argument('--delete', default=False, action='store_true',
                            help=_('delete local packages no longer present in repository'))
        parser.add_argument('-m', '--downloadcomps', default=False, action='store_true',
                            help=_('also download comps.xml'))
        parser.add_argument('--download-metadata', default=False, action='store_true',
                            help=_('download all the metadata.'))
        parser.add_argument('-n', '--newest-only', default=False, action='store_true',
                            help=_('download only newest packages per-repo'))
        parser.add_argument('-p', '--download-path', default='./',
                            help=_('where to store downloaded repositories '))
        parser.add_argument('--source', default=False, action='store_true',
                            help=_('operate on source packages'))

    def configure(self):
        demands = self.cli.demands
        demands.available_repos = True
        demands.sack_activation = True

        repos = self.base.repos

        if self.opts.repo:
            repos.all().disable()
            for repoid in self.opts.repo:
                try:
                    repo = repos[repoid]
                except KeyError:
                    raise dnf.cli.CliError("Unknown repo: '%s'." % repoid)
                repo.enable()

        if self.opts.source:
            repos.enable_source_repos()

        for repo in repos.iter_enabled():
            repo._repo.expire()
            repo.deltarpm = False

    def run(self):
        self.base.conf.keepcache = True
        for repo in self.base.repos.iter_enabled():
            if self.opts.download_metadata:
                self.download_metadata(repo)
            if self.opts.downloadcomps:
                self.getcomps(repo)
            self.download_packages(repo)

    def repo_target(self, repo):
        target = self._repo_target.get(repo.id)
        if not target:
            target = _pkgdir(self.opts.destdir or self.opts.download_path, repo.id)
            self._repo_target[repo.id] = target
        return target

    def pkg_download_path(self, pkg):
        repo_target = self.repo_target(pkg.repo)
        pkg_download_path = os.path.normpath(
            os.path.join(repo_target, pkg.location))
        if not pkg_download_path.startswith(repo_target):
            raise dnf.exceptions.Error(
                _("Download target '{}' is outside of download path '{}'.").format(
                    pkg_download_path, repo_target))
        return pkg_download_path

    def delete_old_local_packages(self, packages_to_download):
        download_map = dict()
        for pkg in packages_to_download:
            download_map[(pkg.repo.id, os.path.basename(pkg.location))] = 1
        # delete any *.rpm file, that is not going to be downloaded from repository
        for repo in self.base.repos.iter_enabled():
            repo_target = self.repo_target(repo)
            if os.path.exists(repo_target):
                for filename in os.listdir(repo_target):
                    path = os.path.join(repo_target, filename)
                    if filename.endswith('.rpm') and os.path.isfile(path):
                        if not (repo.id, filename) in download_map:
                            try:
                                os.unlink(path)
                                logger.info(_("[DELETED] %s"), path)
                            except OSError:
                                logger.error(_("failed to delete file %s"), path)

    def getcomps(self, repo):
        comps_fn = repo.metadata._comps_fn
        if comps_fn:
            dest = os.path.join(self.repo_target(repo), 'comps.xml')
            dnf.yum.misc.decompress(comps_fn, dest=dest)
            logger.info(_("comps.xml for repository %s saved"), repo.id)

    def download_metadata(self, repo):
        repo_target = self.repo_target(repo)
        repo._repo.downloadMetadata(repo_target)
        return True

    def get_pkglist(self, repo):
        query = self.base.sack.query().available().filterm(reponame=repo.id)
        if self.opts.newest_only:
            query = query.latest()
        if self.opts.source:
            query.filterm(arch='src')
        elif self.opts.arches:
            query.filterm(arch=self.opts.arches)
        return query

    def download_packages(self, repo):
        base = self.base
        pkglist = self.get_pkglist(repo)
        progress = base.output.progress
        remote_pkgs, local_repository_pkgs = base._select_remote_pkgs(pkglist)
        if remote_pkgs:
            drpm = dnf.drpm.DeltaInfo(base.sack.query().installed(), progress, 0)
            payloads = [RPMPayloadLocation(pkg, progress, self.pkg_download_path(pkg))
                        for pkg in remote_pkgs]
            base._download_remote_payloads(payloads, drpm, progress, None)
        if local_repository_pkgs:
            for pkg in local_repository_pkgs:
                pkg_path = os.path.join(pkg.repo.pkgdir, pkg.location.lstrip("/"))
                target_dir = os.path.dirname(self.pkg_download_path(pkg))
                dnf.util.ensure_dir(target_dir)
                shutil.copy(pkg_path, target_dir)
        if self.opts.delete:
            self.delete_old_local_packages(pkglist)

