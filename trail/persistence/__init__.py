from .logger import NoLogLogger


def build_logger(backend_name, **kwargs):
    # import inside the if so users do not have to install useless packages
    if backend_name == 'comet_ml':
        from .cometml import CMLLogger

        return CMLLogger(**kwargs)

    return NoLogLogger()

