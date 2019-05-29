
def get_gpu_name():
    import torch
    current_device = torch.cuda.current_device()
    return torch.cuda.get_device_name(current_device)