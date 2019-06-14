import json
import inspect
from typing import Union, Callable, Optional

from argparse import ArgumentParser, Namespace

from benchutils.statstream import StatStream
from track.utils.throttle import throttled
from track.structure import Trial, Project, TrialGroup, set_current_project, set_current_trial

from track.persistence.storage import Storage
from track.logger import Logger
from track.persistence import build_logger, load_storage
from track.serialization import to_json
from track.versioning import default_version_hash
from track.utils.eta import EstimatedTime
from track.configuration import options
from track.utils.out import RingOutputDecorator 


# Client has a lot of methods on purpose. This is our unified API
# pylint: disable=too-many-public-methods
class TrackClient:
    """ TrackClient. A client tracks a single Trial being ran"""

    def __init__(self, backend=options('log.backend.name', default='none'),
                 storage=options('log.storage.protocol', default='file://${project}.json'), **kwargs):

        self.project = None
        self.group = None
        self.trial = Trial()

        set_current_trial(self.trial)

        self.logger_backend = backend
        self.storage_protocol = storage

        self.logger: Logger = Logger(self.trial, build_logger(backend, **kwargs))
        self.eta = EstimatedTime(None, 1)

        # self._system_info()
        # self._version_info()

        self.code = None
        self.stderr = None
        self.stdout = None
        self.batch_printer = None
        self.set_version()
        self.data_store: Optional[Storage] = None

    def set_store(self, store, name=None, force=False):
        """ local store: file://file.json"""
        if name is None and self.project is not None:
            name = self.project.name

        if name is not None:
            store = store.replace('${project}', name)

        if self.data_store is None or force:
            self.data_store = load_storage(store)
        return self

    def set_version(self, version=None, version_fun: Callable[[], str] = None):
        """ compute the version tag from the function call stack """
        if version_fun is None:
            version_fun = default_version_hash

        if version is None:
            self.trial.version = version_fun()
        else:
            self.trial.version = version
        return self

    def set_project(self, project=None, name=None, tags=None, description=None):
        self.set_store(self.storage_protocol, name)

        # Check if the storage for the project
        if name is not None:
            project_id = self.data_store.project_names.get(name)

            if project_id is not None:
                # info('Found project from storage')
                project = self.data_store.objects.get(project_id)
                assert project is not None, \
                    f'Project (id: {project_id}) was found in index but missing in the data table'

        if project is None:
            assert name is not None, 'Project need to have a unique name'
            # info('Creating a new project')
            project = Project(name=name, tags=tags, description=description)
            self.data_store.insert_project(project)

        self.trial.project_id = project.uid
        set_current_project(project)
        self.project = project
        self.logger.set_project(project)
        self.data_store.insert_trial(self.trial)
        return project

    def set_group(self, group=None, name=None, tags=None, description=None):
        if self.project is None:
            raise RuntimeError('Project needs to be set to define a group')

        # look for the group in the project
        for gid in self.project.groups:
            g = self.data_store.objects[gid]
            if g.name == name:
                group = g

        if group is None:
            group = TrialGroup(name=name, tags=tags, description=description)

        group.project_id = self.project.uid
        self.trial.group_id = group.uid

        self.group = group
        self.group.trials.append(self.trial.uid)
        self.logger.set_group(group)
        return group

    def get_arguments(self, args: Union[ArgumentParser, Namespace], show=False) -> Namespace:
        """ Store the arguments that was used to run the trial.  """

        if isinstance(args, ArgumentParser):
            args = args.parse_args()

        self.logger.log_arguments(args)

        if show:
            print('-' * 80)
            for k, v in vars(args).items():
                print(f'{k:>30}: {v}')
            print('-' * 80)

        return args

    def __getattr__(self, item):
        """ try to use the backend attributes if not available """
        # def chainer(fun):
        #     def _chainer(*args, **kwargs):
        #         fun(*args, **kwargs)
        #         return self
        #     return _chainer

        # Look for the attribute in the top level logger
        if hasattr(self.logger, item):
            return getattr(self.logger, item)

        # Look for the attribute in the backend
        if hasattr(self.logger.backend, item):
            return getattr(self.logger.backend, item)

        raise AttributeError(item)

    def log_code(self):
        self.code = open(inspect.stack()[-1].filename, 'r').read()
        return self

    def show_eta(self, step: int, timer: StatStream, msg: str = '',
                 throttle=options('log.print.throttle', None),
                 every=options('log.print.every', None),
                 no_print=options('log.print.disable', False)):

        self.eta.timer = timer

        if self.batch_printer is None:
            self.batch_printer = throttled(self.eta.show_eta, throttle, every)

        if not no_print:
            self.batch_printer(step, msg)
        return self

    def report(self, short=True):
        """ print a digest of the logged metrics """
        self.logger.finish()
        print(json.dumps(to_json(self.trial, short), indent=2))
        return self

    def save(self, file_name_override=None):
        """ saved logged metrics into a json file """
        self.data_store.commit(file_name_override)

        # if file_name is None:
        #     warning('No output file specified')
        #     return
        #
        # initial_data = []
        # if os.path.exists(file_name):
        #     initial_data = json.load(open(file_name, 'r'))
        #
        # if self.project is not None:
        #     initial_data.append(to_json(self.project))
        # else:
        #     initial_data.append(to_json(self.trial))
        #
        # with open(file_name, 'w') as out:
        #     json.dump(initial_data, out, indent=2)
        # return self

    @staticmethod
    def get_device():
        """ helper function that returns a cuda device if available else a cpu"""
        import torch

        if torch.cuda.is_available():
            return torch.device('cuda')
        return torch.device('cpu')

    # -- Getter Setter
    def set_eta_total(self, t):
        self.eta.set_totals(t)
        return self

    def capture_output(self):
        import sys
        do_stderr = sys.stderr is not sys.stdout

        self.stdout = RingOutputDecorator(file=sys.stdout, n_entries=options('log.stdout_capture', 50))
        sys.stdout = self.stdout

        if do_stderr:
            self.stderr = RingOutputDecorator(file=sys.stderr, n_entries=options('log.stderr_capture', 50))
            sys.stderr = self.stderr
        return self

    def finish(self, exc_type=None, exc_val=None, exc_tb=None):
        return self.logger.finish(exc_type, exc_val, exc_tb)

    def start(self):
        return self.logger.start()

    def __enter__(self):
        return self.logger.__enter__()

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self.logger.__exit__(exc_type, exc_val, exc_tb)
