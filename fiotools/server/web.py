#!/usr/bin/env python3

import cherrypy
from cherrypy.process import plugins
from cherrypy.lib.httputil import parse_query_string  # HTTPDate
# import threading
import queue
# import time
import sys
import os
import json
import sqlite3
import uuid
import datetime
import time
# import requests
# import logging

import glob
from ..utils import get_pid_file, rfile
from ..reports import latency_summary
# from ..db import FIOdatabase

DBNAME = os.path.join(os.path.expanduser('~'), 'fioservice.db')
JOB_DIR = "./data/fio/jobs"

log = cherrypy.log
# logging.getLogger('cherrypy').propagate = False

job_active = False
work_queue = queue.Queue()
active_job = None


def fetch_all(table, keys):
    assert isinstance(keys, list)
    if not keys:
        return list()

    data = list()
    with sqlite3.connect(DBNAME) as c:
        c.row_factory = sqlite3.Row
        csr = c.cursor()
        fields = ",".join(keys)
        csr.execute(""" SELECT {} FROM {};""".format(fields, table))

    rows = csr.fetchall()
    for row in rows:
        data.append({k: row[k] for k in keys})

    return data


def fetch_row(table, key=None, content=None):
    response = dict()
    if not key:
        return response
    available = [row[key] for row in fetch_all(table, list([key]))]
    if not content or content not in available:
        return response
    else:
        query = "SELECT * FROM {} WHERE {} = ?;".format(table, key)
        with sqlite3.connect(DBNAME) as c:
            c.row_factory = sqlite3.Row
            csr = c.cursor()
            csr.execute(query, (content,))
            row = csr.fetchall()[0]

        # convert the row object to a dict
        response = {k: row[k] for k in row.keys()}
        return response


class AsyncJob(object):
    pass


def setup_db(dbname=DBNAME):

    profiles_table = """ CREATE TABLE IF NOT EXISTS profiles (
                            name text PRIMARY KEY,
                            spec text,
                            created integer,
                            updated integer
                            ); """
    jobs_table = """ CREATE TABLE IF NOT EXISTS jobs (
                        id text PRIMARY KEY,
                        title text NOT NULL,
                        profile text NOT NULL,
                        workers integer NOT NULL,
                        status text NOT NULL,
                        started integer,
                        ended integer,
                        raw_json text,
                        summary text,
                        type text,
                        raw_output text,
                        provider text,
                        platform text,
                        FOREIGN KEY(profile) REFERENCES profiles (name)
                        ); """

    if not os.path.exists(dbname):
        print("Creating results database")
        with sqlite3.connect(dbname) as c:
            c.execute(profiles_table)
            c.execute(jobs_table)


def load_db_profiles(jobdir=JOB_DIR, dbname=DBNAME, out='console'):
    def message(msg_string, out='console'):
        if out == 'console':
            print(msg_string)
        else:
            cherrypy.log(msg_string)

    changes = {
        "processed": [],
        "new": [],
        "changed": [],
        "skipped": [],
    }

    message("Refreshing job profiles", out)
    with sqlite3.connect(dbname) as c:
        c.row_factory = sqlite3.Row
        cursor = c.cursor()
        for profile in glob.glob('{}/*'.format(jobdir)):
            name = os.path.basename(profile)
            changes['processed'].append(profile)
            profile_spec = rfile(profile)
            cursor.execute("SELECT * from profiles WHERE name=?;",
                           (name,))
            data = cursor.fetchone()
            if data is None:
                message("- loading profile {}".format(name), out)
                changes['new'].append(profile)
                now = int(datetime.datetime.now().strftime("%s"))
                cursor.execute("INSERT INTO profiles VALUES (?,?,?,?);",
                                (name, profile_spec, now, now))  # NOQA
            else:
                # if spec is the same - just skip it
                if data['spec'] == profile_spec:
                    message("- skipping identical profile {}".format(name), out)
                    changes['skipped'].append(profile)
                else:
                    # if not, apply the filesystem copy to the database
                    message("- refreshing profile '{}' in the db with filesystem copy".format(name), out)
                    changes['changed'].append(profile)
                    cursor.execute(""" UPDATE profiles
                                            SET
                                            spec=?,
                                            updated=?
                                            WHERE
                                            name=?;""",
                                   (profile_spec, int(datetime.datetime.now().strftime("%s")), name))
    return changes

@cherrypy.expose
class Root(object):
    pass


class APIroot(object):
    exposed = True

    # TODO add a get method to document the API
    def __init__(self, service_state):  # , handler):
        self.job = Job(service_state)  # handler)
        self.profile = Profile(service_state)  # handler)
        self.status = Status(service_state)  # Web service metadata info


def run_job(handler, service_state):

    if not work_queue.empty():

        job = work_queue.get()
        service_state.task_active = True
        service_state.tasks_queued = work_queue.qsize
        if job.type == 'startfio':
            service_state.active_job_type = 'FIO'
            with sqlite3.connect(DBNAME) as c:
                csr = c.cursor()
                csr.execute(""" UPDATE jobs
                                SET status = ?,
                                    started = ?
                                WHERE
                                    id = ?;""", ('started', int(datetime.datetime.now().strftime("%s")),
                                                 job.uuid)
                            )
            cherrypy.log("job {} start".format(job.uuid))
            runrc = handler.startfio(job.profile, job.workers, job.outfile)
            if runrc == 0:
                cherrypy.log("job {} completed successfully".format(job.uuid))
                cherrypy.log("job {} fetching report data".format(job.uuid))
                fetchrc = handler.fetch_report(job.outfile)

                if fetchrc == 0:
                    cherrypy.log("job {} adding job results to db".format(job.uuid))
                    job_output = rfile('/tmp/{}'.format(job.outfile))

                    # ignore any errors in the json - present at the start
                    job_data = job_output[job_output.find('{', 0):]

                    job_json = json.loads(job_data)
                    summary = latency_summary(job_json)  # use default percentile
                    with sqlite3.connect(DBNAME) as c:
                        csr = c.cursor()
                        csr.execute(""" UPDATE jobs
                                        SET status = ?,
                                            ended = ?,
                                            raw_json = ?,
                                            summary = ?
                                        WHERE
                                            id = ?;""", ('complete', int(datetime.datetime.now().strftime("%s")),
                                                         job_output,
                                                         json.dumps(summary),
                                                         job.uuid)
                                    )
                    cherrypy.log("job {} finished".format(job.uuid))
            else:
                cherrypy.log("job {} failed with rc={}".format(job.uuid, runrc))
                # update state of job with failure
                with sqlite3.connect(DBNAME) as c:
                    csr = c.cursor()
                    csr.execute(""" UPDATE jobs
                                    SET status = ?,
                                        ended = ?
                                    WHERE
                                        id = ?;""", ('failed', int(datetime.datetime.now().strftime("%s")),
                                                     job.uuid)
                                )

        else:
            cherrypy.log("WARNING: unknown job type requested - {} - ignoring".format(job.type))
        service_state.task_active = False
        service_state.active_job_type = 'N/A'


class Status(object):
    exposed = True

    def __init__(self, service_state):
        self.service_state = service_state

    @cherrypy.tools.json_out()
    def GET(self):
        # keep the data returned fast so it can be polled easily and quickly
        run_time = time.time() - self.service_state.start_time
        return {
            "data": {
                "target": self.service_state.target,
                "task_active": self.service_state.task_active,
                "tasks_queued": self.service_state.tasks_queued,
                "task_type": self.service_state.active_job_type,
                "run_time": run_time,
                "workers": self.service_state._handler.workers
            }
        }


# @cherrypy.expose
class Job(object):
    exposed = True

    def __init__(self, service_state):
        self.service_state = service_state

    @cherrypy.tools.json_in()
    @cherrypy.tools.json_out()
    def GET(self, uuid=None, **params):
        qs = parse_query_string(cherrypy.request.query_string)
        # cherrypy.log(json.dumps(qs))
        if not qs or not qs.get('fields', None):
            fields = list(['id', 'status', 'title'])
        else:
            fields = qs['fields'].split(',')

        if uuid is None:
            return {"data": fetch_all('jobs', fields)}
        else:
            data = fetch_row('jobs', 'id', uuid)
            if data:
                return {"data": json.dumps(fetch_row('jobs', 'id', uuid))}
            else:
                raise cherrypy.HTTPError(404, "Invalid job id")

    # @cherrypy.tools.accept(media='application/json')
    @cherrypy.tools.json_in()
    @cherrypy.tools.json_out()
    def POST(self, profile, **params):
        # required payload
        required = ['title', 'provider', 'platform']

        js_in = cherrypy.request.json
        qs_in = parse_query_string(cherrypy.request.query_string)
        cherrypy.log("DEBUG: qs = {}".format(json.dumps(qs_in)))
        cherrypy.log("DEBUG: payload = {}".format(json.dumps(js_in)))

        parms = {**js_in, **qs_in}

        if not all([k in parms.keys() for k in required]):
            raise cherrypy.HTTPError(400, "missing fields in request")

        available_profiles = [p['name'] for p in fetch_all('profiles', list(['name']))]
        if profile not in available_profiles:
            # raise APIError(status=404, message="FIO workload profile '{}' not found in ./data/fio/jobs".format(profile))
            raise cherrypy.HTTPError(404, "profile not found")
        cherrypy.log("profile is {}".format(profile))

        job = AsyncJob()
        job.type = 'startfio'
        job.uuid = str(uuid.uuid4())
        job.profile = profile
        job.outfile = '{}.{}'.format(job.uuid, profile)
        job.workers = parms.get('workers', 9999)
        job.provider = parms.get('provider')
        job.platform = parms.get('platform')
        job.title = parms.get('title', '{} on '.format(profile, datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S")))

        with sqlite3.connect(DBNAME) as c:
            csr = c.cursor()

            csr.execute(""" INSERT into jobs (id, title, profile, workers, status, type, provider, platform)
                              VALUES(?,?,?,?,?,?,?,?);""",
                        (job.uuid,
                         job.title,
                         profile,
                         job.workers,
                         'queued',
                         'fio',
                         job.provider,
                         job.platform,
                         ))

        # post request using a valid profile, place on the queue to run
        work_queue.put(job)
        self.service_state.tasks_queued = work_queue.qsize()

        return {'data': {"message": "run requested, current work queue size {}".format(work_queue.qsize()),
                         "uuid": job.uuid}}

    @cherrypy.tools.json_out()
    def DELETE(self, uuid=None):
        # TODO
        # delete a specific job from the database
        return {"data": {"msg": "not implemented yet!"}}

# @cherrypy.tools.accept(media='application/json')
@cherrypy.expose
class Profile(object):

    def __init__(self, service_state):
        self.service_state = service_state

    @cherrypy.tools.json_out()
    def GET(self, profile=None):
        if profile is None:
            return {"data": fetch_all('profiles', list(['name']))}
        else:
            return {"data": fetch_row('profiles', 'name', profile)['spec']}

    @cherrypy.tools.json_out()
    def PUT(self):
        summary = load_db_profiles(out='cherrypy')

        if summary['new'] or summary['changed']:
            # TODO: need to sync the fiomgr pod with these changes
            # by submitting a background job
            pass

        return {"data": {"summary": summary}}


def CORS():
    # enable CORS for all origins
    cherrypy.response.headers["Access-Control-Allow-Origin"] = "*"


class ServiceStatus(object):
    def __init__(self, handler):
        self._handler = handler
        self.target = self._handler._target
        self.task_active = False
        self.tasks_queued = 0
        self.active_job_type = None
        self.job_count = 0
        self.profile_count = 0
        self.start_time = time.time()


class FIOWebService(object):

    def __init__(self, handler=None, workdir=None, port=8080):
        self.handler = handler
        self.service_state = ServiceStatus(handler=handler)
        self.port = port
        self.root = Root()         # web UI
        self.root.api = APIroot(self.service_state)  # API

        if workdir:
            self.workdir = workdir
        else:
            self.workdir = os.path.expanduser("~")

        self.worker = None  # long running worker thread - FIO JOBS
        self.conf = {
            'global': {
                'log.screen': False,
                'error_file': '',
                'access_file': '',
            },
            '/': {
                'request.show_tracebacks': False,
                'request.dispatch': cherrypy.dispatch.MethodDispatcher(),
                'tools.response_headers.on': True,
                'tools.staticdir.on': True,
                'tools.staticdir.dir': os.path.join(os.getcwd(), 'www'),
                'tools.staticdir.index': 'index.html',
                'tools.CORS.on': True,
            },
        }
        cherrypy.tools.CORS = cherrypy.Tool('before_handler', CORS)

        setup_db()

        load_db_profiles()

    def cleanup(self):
        # cancel the worker background thread based on current interval
        self.worker.cancel()

    @property
    def ready(self):

        # command missing
        if not self.handler._can_run:
            return False

        # no external connection
        if not self.handler.has_connection:
            return False

        # no profiles in the db
        if not fetch_all('profiles', ['name']):
            return False

        # use the handler to determine the number of workers
        rc = self.handler.num_workers()
        if self.handler.workers == 0 or rc != 0:
            return False

        return True

    def run(self):

        self.service_state.workers = self.handler.workers

        daemon = plugins.Daemonizer(
            cherrypy.engine,
            stdout=os.path.join(self.workdir, 'fioservice.access.log'),
            stderr=os.path.join(self.workdir, 'fioservice.log'),
            )

        daemon.subscribe()
        cherrypy.log.error_log.propagate = False
        cherrypy.log.access_log.propagate = False
        cherrypy.server.socket_host = '0.0.0.0'
        cherrypy.tree.mount(self.root, config=self.conf)
        cherrypy.config.update({
            'engine.autoreload.on': False,
            'server.socket_host': '0.0.0.0',
            'server.socket_port': self.port
        })

        plugins.PIDFile(cherrypy.engine, get_pid_file(self.workdir)).subscribe()
        plugins.SignalHandler(cherrypy.engine).subscribe()  # handle SIGTERM, SIGHUP etc

        self.worker = plugins.BackgroundTask(interval=1, function=run_job, args=[self.handler, self.service_state])
        cherrypy.engine.subscribe('stop', self.cleanup)

        # prevent CP loggers from propagating entries to the root logger
        # (i.e stop duplicate log entries)

        try:
            cherrypy.engine.start()
        except Exception:
            sys.exit(1)
        else:
            self.worker.run()
            cherrypy.engine.block()


if __name__ == '__main__':

    svr = FIOWebService()
    svr.run()
