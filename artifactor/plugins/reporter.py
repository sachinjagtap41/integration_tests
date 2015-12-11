# -*- coding: utf-8 -*-
""" Reporter plugin for Artifactor

Add a stanza to the artifactor config like this,
artifactor:
    log_dir: /home/username/outdir
    per_run: test #test, run, None
    reuse_dir: True
    plugins:
        reporter:
            enabled: True
            plugin: reporter
            only_failed: False #Only show faled tests in the report
"""
import csv
import datetime
import difflib
import math
import os
import re
import shutil
import time
from copy import deepcopy

from jinja2 import Environment, FileSystemLoader
from py.path import local

from utils.conf import cfme_data  # Only for the provider specific reports
from utils.path import template_path
from artifactor import ArtifactorBasePlugin

_tests_tpl = {
    '_sub': {},
    '_stats': {
        'passed': 0,
        'failed': 0,
        'skipped': 0,
        'error': 0,
        'xpassed': 0,
        'xfailed': 0
    },
    '_duration': 0
}

# Regexp, that finds all URLs in a string
# Does not cover all the cases, but rather only those we can
URL = re.compile(r"https?://[^/\s]+(?:/[^/\s?]+)*/?(?:\?(?:[^&\s=]+(?:=[^&\s]+)?&?)*)?")


def overall_test_status(statuses):
    # Handle some logic for when to count certain tests as which state
    for when, status in statuses.iteritems():
        if when == "call" and status[1] and status[0] == "skipped":
            return "xfailed"
        elif when == "call" and status[1] and status[0] == "failed":
            return "xpassed"
        elif (when == "setup" or when == "teardown") and status[0] == "failed":
            return "error"
        elif status[0] == "skipped":
            return "skipped"
        elif when == "call" and status[0] == 'failed':
            return "failed"
    return "passed"


class Reporter(ArtifactorBasePlugin):

    def plugin_initialize(self):
        self.register_plugin_hook('report_test', self.report_test)
        self.register_plugin_hook('finish_session', self.run_report)
        self.register_plugin_hook('finish_session', self.run_provider_report)
        self.register_plugin_hook('build_report', self.run_report)
        self.register_plugin_hook('start_test', self.start_test)
        self.register_plugin_hook('skip_test', self.skip_test)
        self.register_plugin_hook('finish_test', self.finish_test)
        self.register_plugin_hook('session_info', self.session_info)
        self.register_plugin_hook('composite_pump', self.composite_pump)

    def configure(self):
        self.only_failed = self.data.get('only_failed', False)
        self.configured = True

    @ArtifactorBasePlugin.check_configured
    def composite_pump(self, old_artifacts):
        return None, {'old_artifacts': old_artifacts}

    @ArtifactorBasePlugin.check_configured
    def skip_test(self, test_location, test_name, skip_data):
        test_ident = "{}/{}".format(test_location, test_name)
        return None, {'artifacts': {test_ident: {'skipped': skip_data}}}

    @ArtifactorBasePlugin.check_configured
    def start_test(self, test_location, test_name, slaveid):
        test_ident = "{}/{}".format(test_location, test_name)
        return None, {'artifacts': {test_ident: {'start_time': time.time(), 'slaveid': slaveid}}}

    @ArtifactorBasePlugin.check_configured
    def finish_test(self, test_location, test_name, slaveid):
        test_ident = "{}/{}".format(test_location, test_name)
        return None, {'artifacts': {test_ident: {'finish_time': time.time(), 'slaveid': slaveid}}}

    @ArtifactorBasePlugin.check_configured
    def report_test(self, artifacts, test_location, test_name, test_xfail, test_when, test_outcome):
        test_ident = "{}/{}".format(test_location, test_name)
        return None, {'artifacts': {test_ident: {'statuses': {
            test_when: (test_outcome, test_xfail)}}}}

    @ArtifactorBasePlugin.check_configured
    def session_info(self, version=None):
        return None, {'version': version}

    @ArtifactorBasePlugin.check_configured
    def run_report(self, old_artifacts, artifact_dir, version=None):
        template_data = self.process_data(old_artifacts, artifact_dir, version)

        if self.only_failed:
            template_data['tests'] = [x for x in template_data['tests']
                                  if x['outcomes']['overall'] not in ['passed']]

        self.render_report(template_data, 'report', artifact_dir, 'test_report.html')

    @ArtifactorBasePlugin.check_configured
    def run_provider_report(self, artifacts, artifact_dir, version=None):
        for mgmt in cfme_data['management_systems'].keys():
            template_data = self.process_data(artifacts, artifact_dir, version, name_filter=mgmt)

            self.render_report(template_data, "report_{}".format(mgmt), artifact_dir,
                               'test_report_provider.html')

    def render_report(self, report, filename, log_dir, template):
        template_env = Environment(
            loader=FileSystemLoader(template_path.strpath)
        )
        data = template_env.get_template(template).render(**report)

        with open(os.path.join(log_dir, '{}.html'.format(filename)), "w") as f:
            f.write(data)
        try:
            shutil.copytree(template_path.join('dist').strpath, os.path.join(log_dir, 'dist'))
        except OSError:
            pass

    def process_data(self, artifacts, log_dir, version, name_filter=None):
        tb_errors = []
        blocker_skip_count = 0
        provider_skip_count = 0
        template_data = {'tests': [], 'qa': []}
        template_data['version'] = version
        log_dir = local(log_dir).strpath + "/"
        counts = {
            'passed': 0,
            'failed': 0,
            'skipped': 0,
            'error': 0,
            'xfailed': 0,
            'xpassed': 0}
        current_counts = {
            'passed': 0,
            'failed': 0,
            'skipped': 0,
            'error': 0,
            'xfailed': 0,
            'xpassed': 0}
        colors = {
            'passed': 'success',
            'failed': 'warning',
            'error': 'danger',
            'xpassed': 'danger',
            'xfailed': 'success',
            'skipped': 'info'}
        # Iterate through the tests and process the counts and durations
        for test_name, test in artifacts.iteritems():
            if not test.get('statuses', None):
                continue
            overall_status = overall_test_status(test['statuses'])
            counts[overall_status] += 1
            if not test.get('old', False):
                current_counts[overall_status] += 1
            color = colors[overall_status]
            # Set the overall status and then process duration
            test['statuses']['overall'] = overall_status
            test_data = {'name': test_name, 'outcomes': test['statuses'],
                         'slaveid': test.get('slaveid', "Unknown"), 'color': color}
            if 'composite' in test:
                test_data['composite'] = test['composite']

            if 'skipped' in test:
                if test['skipped'].get('type', None) == 'provider':
                    provider_skip_count += 1
                    test_data['skip_provider'] = test['skipped'].get('reason', None)
                if test['skipped'].get('type', None) == 'blocker':
                    blocker_skip_count += 1
                    test_data['skip_blocker'] = test['skipped'].get('reason', None)

            if test.get('old', False):
                test_data['old'] = True

            if test.get('start_time', None):
                if test.get('finish_time', None):
                    test_data['in_progress'] = False
                    test_data['duration'] = test['finish_time'] - test['start_time']
                else:
                    test_data['duration'] = time.time() - test['start_time']
                    test_data['in_progress'] = True

            # Set up destinations for the files
            for ident in test.get('files', []):
                if "softassert" in ident:
                    clean_files = []
                    for assertion in test['files']['softassert']:
                        files = {k: v.replace(log_dir, "") for k, v in assertion.iteritems()}
                        clean_files.append(files)
                    test_data['softassert'] = sorted(clean_files)
                    continue

                for filename in test['files'].get(ident, []):
                    if "rbac_screenshot" in filename:
                        test_data['rbac_screenshot'] = filename.replace(log_dir, "")
                    elif "screenshot" in filename:
                        test_data['screenshot'] = filename.replace(log_dir, "")
                    elif "short-traceback" in filename:
                        short_tb_data = open(filename).read()
                        test_data['short_tb'] = short_tb_data
                        tb_errors.append((short_tb_data, test_name))
                    elif "rbac-traceback" in filename:
                        test_data['rbac'] = filename.replace(log_dir, "")
                    elif "traceback" in filename:
                        test_data['full_tb'] = filename.replace(log_dir, "")
                    elif "video" in filename:
                        test_data['video'] = filename.replace(log_dir, "")
                    elif "cfme.log" in filename:
                        test_data['cfme'] = filename.replace(log_dir, "")
                    elif "function" in filename:
                        test_data['function'] = filename.replace(log_dir, "")
                    elif "emails.html" in filename:
                        test_data['emails'] = filename.replace(log_dir, "")
                    elif "events.html" in filename:
                        test_data['event_testing'] = filename.replace(log_dir, "")
                    elif "qa_contact.txt" in filename:
                        test_data['qa_contact'] = []
                        with open(filename, 'rb') as qafile:
                            qareader = csv.reader(qafile, delimiter=',', quotechar='"')
                            for qacontact in qareader:
                                test_data['qa_contact'].append(qacontact)
                                if qacontact[0] not in template_data['qa']:
                                    template_data['qa'].append(qacontact[0])
                if "merkyl" in ident:
                    test_data['merkyl'] = [f.replace(log_dir, "")
                                           for f in test['files']['merkyl']]

            if "short_tb" in test_data and test_data["short_tb"]:
                urls = [url for url in URL.findall(test_data["short_tb"])]
                if urls:
                    test_data["urls"] = urls
            template_data['tests'].append(test_data)
        template_data['top10'] = self.top10(tb_errors)
        template_data['counts'] = counts
        template_data['current_counts'] = current_counts
        template_data['blocker_skip_count'] = blocker_skip_count
        template_data['provider_skip_count'] = provider_skip_count

        if name_filter:
            template_data['tests'] = [x for x in template_data['tests']
                                      if re.findall('{}[-\]]+'.format(name_filter), x['name'])]

        # Create the tree dict that is used for js tree
        # Note template_data['tests'] != tests
        tests = deepcopy(_tests_tpl)
        tests['_sub']['tests'] = deepcopy(_tests_tpl)

        for test in template_data['tests']:
            self.build_dict(test['name'].replace('cfme/', ''), tests, test)

        template_data['ndata'] = self.build_li(tests)

        for test in template_data['tests']:
            if test.get('duration', None):
                test['duration'] = str(datetime.timedelta(
                    seconds=math.ceil(test['duration'])))

        return template_data

    def top10(self, tb_errors):
        sets = []
        for entry in tb_errors:
            for tset in sets:
                if difflib.SequenceMatcher(a=entry[0][:10], b=tset[0][0][:10]).ratio() > .8:
                    if difflib.SequenceMatcher(a=entry[0][:20], b=tset[0][0][:20]).ratio() > .75:
                        if difflib.SequenceMatcher(a=entry[0][:30], b=tset[0][0][:30]).ratio() > .7:
                            tset.append(entry)
                            break
            else:
                sets.append([entry])

        return sorted(sets, lambda p, q: cmp(len(p), len(q)), reverse=True)[:10]

    def build_dict(self, path, container, contents):
        """
        Build a hierarchical dictionary including information about the stats at each level
        and the duration.
        """
        segs = path.split('/')
        head = segs[0]
        end = segs[1:]

        # If we are at the end node, ie a test.
        if not end:
            container['_sub'][head] = contents
            container['_stats'][contents['outcomes']['overall']] += 1
            container['_duration'] += contents['duration']
        # If we are in a module.
        else:
            if head not in container['_sub']:
                container['_sub'][head] = deepcopy(_tests_tpl)
            # Call again to recurse down the tree.
            self.build_dict('/'.join(end), container['_sub'][head], contents)
            container['_stats'][contents['outcomes']['overall']] += 1
            container['_duration'] += contents['duration']

    def build_li(self, lev):
        """
        Build up the actual HTML tree from the dict from build_dict
        """
        bimdict = {'passed': 'success',
                   'failed': 'warning',
                   'error': 'danger',
                   'skipped': 'primary',
                   'xpassed': 'danger',
                   'xfailed': 'success'}
        list_string = '<ul>\n'
        for k, v in lev['_sub'].iteritems():

            # If 'name' is an attribute then we are looking at a test (leaf).
            if 'name' in v:
                pretty_time = str(datetime.timedelta(seconds=math.ceil(v['duration'])))
                teststring = '<span name="mod_lev" class="label label-primary">T</span>'
                label = '<span class="label label-{}">{}</span>'.format(
                    bimdict[v['outcomes']['overall']], v['outcomes']['overall'].upper())
                link = ('<a href="#{}">{} {} {} '
                        '<span style="color:#888888"><em>[{}]</em></span></a>').format(
                    v['name'], os.path.split(v['name'])[1], teststring, label, pretty_time)
                list_string += '<li>{}</li>\n'.format(link)

            # If there is a '_sub' attribute then we know we have other modules to go.
            elif '_sub' in v:
                percenstring = ""
                bmax = 0
                for _, val in v['_stats'].iteritems():
                    bmax += val
                # If there were any NON skipped tests, we now calculate the percentage which
                # passed.
                if bmax:
                    percen = "{:.2f}".format((float(v['_stats']['passed']) +
                                              float(v['_stats']['skipped']) +
                                              float(v['_stats']['xfailed'])) / float(bmax) * 100)
                    if float(percen) == 100.0:
                        level = 'passed'
                    elif float(percen) > 80.0:
                        level = 'failed'
                    else:
                        level = 'error'
                    percenstring = '<span name="blab" class="label label-{}">{}%</span>'.format(
                        bimdict[level], percen)
                modstring = '<span name="mod_lev" class="label label-primary">M</span>'
                pretty_time = str(datetime.timedelta(seconds=math.ceil(v['_duration'])))
                list_string += ('<li>{} {}<span>&nbsp;</span>'
                                '{}{}<span style="color:#888888">&nbsp;<em>[{}]'
                                '</em></span></li>\n').format(k,
                                                              modstring,
                                                              str(percenstring),
                                                              self.build_li(v),
                                                              pretty_time)
        list_string += '</ul>\n'
        return list_string
