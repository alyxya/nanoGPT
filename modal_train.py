import modal
from dataclasses import dataclass, asdict
from typing import Optional


app = modal.App()

# Create volumes for profiling traces and model outputs
traces = modal.Volume.from_name("nanogpt-traces", create_if_missing=True)
checkpoints = modal.Volume.from_name("nanogpt-checkpoints", create_if_missing=True)
TRACE_DIR = "/traces"
CHECKPOINT_DIR = "/checkpoints"

image = (
    modal.Image.debian_slim()
    .pip_install("torch", "numpy", "transformers", "tiktoken", "wandb", "tqdm")
    .apt_install("git")
    .add_local_file("model.py", "/root/model.py")
    .add_local_dir("config", "/root/config")
    .add_local_dir("data", "/root/data")
)

@app.function(gpu="A100", image=image, timeout=3600*12, volumes={TRACE_DIR: traces, CHECKPOINT_DIR: checkpoints})  # 12 hour timeout for long training
def train(config_file=None, enable_profiling=False):
    """
    This training script can be run both on a single gpu in debug mode,
    and also in a larger training run with distributed data parallel (ddp).

    To run on a single GPU, example:
    $ python train.py --batch_size=32 --compile=False

    To run with DDP on 4 gpus on 1 node, example:
    $ torchrun --standalone --nproc_per_node=4 train.py

    To run with DDP on 4 gpus across 2 nodes, example:
    - Run on the first (master) node with example IP 123.456.123.456:
    $ torchrun --nproc_per_node=8 --nnodes=2 --node_rank=0 --master_addr=123.456.123.456 --master_port=1234 train.py
    - Run on the worker node:
    $ torchrun --nproc_per_node=8 --nnodes=2 --node_rank=1 --master_addr=123.456.123.456 --master_port=1234 train.py
    (If your cluster does not have Infiniband interconnect prepend NCCL_IB_DISABLE=1)
    """

    import os
    import time
    import math
    import pickle
    from contextlib import nullcontext

    import numpy as np
    import torch
    from torch.nn.parallel import DistributedDataParallel as DDP
    from torch.distributed import init_process_group, destroy_process_group

    from model import GPTConfig, GPT

    @dataclass
    class TrainingConfig:
        # I/O
        out_dir: str = '/checkpoints/out'
        eval_interval: int = 2000
        log_interval: int = 1
        eval_iters: int = 200
        eval_only: bool = False
        always_save_checkpoint: bool = True
        init_from: str = 'scratch'
        # wandb logging
        wandb_log: bool = False
        wandb_project: str = 'owt'
        wandb_run_name: str = 'gpt2'
        # data
        dataset: str = 'openwebtext'
        gradient_accumulation_steps: int = 5 * 8
        batch_size: int = 12
        block_size: int = 1024
        # model
        n_layer: int = 12
        n_head: int = 12
        n_embd: int = 768
        dropout: float = 0.0
        bias: bool = False
        # adamw optimizer
        learning_rate: float = 6e-4
        max_iters: int = 600000
        weight_decay: float = 1e-1
        beta1: float = 0.9
        beta2: float = 0.95
        grad_clip: float = 1.0
        # learning rate decay settings
        decay_lr: bool = True
        warmup_iters: int = 2000
        lr_decay_iters: int = 600000
        min_lr: float = 6e-5
        # DDP settings
        backend: str = 'nccl'
        # system
        device: str = 'cuda'
        dtype: str = 'bfloat16' if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else 'float16'
        compile: bool = True

        @classmethod
        def from_file(cls, config_file: str) -> 'TrainingConfig':
            """Load config from a Python file."""
            config_namespace = {}
            with open(config_file) as f:
                config_content = f.read()

            # Execute the config file in an isolated namespace
            exec(config_content, {}, config_namespace)

            # Filter out private variables and functions
            config_values = {k: v for k, v in config_namespace.items()
                           if not k.startswith('_') and not callable(v)}

            # Create config instance with overrides
            return cls(**config_values)

    # Load configuration
    if config_file:
        print(f"Loading config from {config_file}")
        config = TrainingConfig.from_file(config_file)
        print(f"Config loaded: {asdict(config)}")
    else:
        config = TrainingConfig()
        print("Using default configuration")
    
    # Ensure out_dir is always within the checkpoints volume
    if not config.out_dir.startswith(CHECKPOINT_DIR):
        # Extract the final directory name from the original out_dir
        out_dir_name = os.path.basename(config.out_dir.rstrip('/'))
        config.out_dir = os.path.join(CHECKPOINT_DIR, out_dir_name)
        print(f"Adjusted out_dir to use checkpoint volume: {config.out_dir}")

    # Extract config values for easier access
    out_dir = config.out_dir
    eval_interval = config.eval_interval
    log_interval = config.log_interval
    eval_iters = config.eval_iters
    eval_only = config.eval_only
    always_save_checkpoint = config.always_save_checkpoint
    init_from = config.init_from
    wandb_log = config.wandb_log
    wandb_project = config.wandb_project
    wandb_run_name = config.wandb_run_name
    dataset = config.dataset
    gradient_accumulation_steps = config.gradient_accumulation_steps
    batch_size = config.batch_size
    block_size = config.block_size
    n_layer = config.n_layer
    n_head = config.n_head
    n_embd = config.n_embd
    dropout = config.dropout
    bias = config.bias
    learning_rate = config.learning_rate
    max_iters = config.max_iters
    weight_decay = config.weight_decay
    beta1 = config.beta1
    beta2 = config.beta2
    grad_clip = config.grad_clip
    decay_lr = config.decay_lr
    warmup_iters = config.warmup_iters
    lr_decay_iters = config.lr_decay_iters
    min_lr = config.min_lr
    backend = config.backend
    device = config.device
    dtype = config.dtype
    compile = config.compile

    # Convert config to dict for logging
    config_dict = asdict(config)
    # -----------------------------------------------------------------------------

    # various inits, derived attributes, I/O setup
    ddp = int(os.environ.get('RANK', -1)) != -1 # is this a ddp run?
    if ddp:
        init_process_group(backend=backend)
        ddp_rank = int(os.environ['RANK'])
        ddp_local_rank = int(os.environ['LOCAL_RANK'])
        ddp_world_size = int(os.environ['WORLD_SIZE'])
        device = f'cuda:{ddp_local_rank}'
        torch.cuda.set_device(device)
        master_process = ddp_rank == 0 # this process will do logging, checkpointing etc.
        seed_offset = ddp_rank # each process gets a different seed
        # world_size number of processes will be training simultaneously, so we can scale
        # down the desired gradient accumulation iterations per process proportionally
        assert gradient_accumulation_steps % ddp_world_size == 0
        gradient_accumulation_steps //= ddp_world_size
    else:
        # if not ddp, we are running on a single gpu, and one process
        master_process = True
        seed_offset = 0
        ddp_world_size = 1
    tokens_per_iter = gradient_accumulation_steps * ddp_world_size * batch_size * block_size
    print(f"tokens per iteration will be: {tokens_per_iter:,}")

    if master_process:
        os.makedirs(out_dir, exist_ok=True)
    torch.manual_seed(1337 + seed_offset)
    torch.backends.cuda.matmul.allow_tf32 = True # allow tf32 on matmul
    torch.backends.cudnn.allow_tf32 = True # allow tf32 on cudnn
    device_type = 'cuda' if 'cuda' in device else 'cpu' # for later use in torch.autocast
    # note: float16 data type will automatically use a GradScaler
    ptdtype = {'float32': torch.float32, 'bfloat16': torch.bfloat16, 'float16': torch.float16}[dtype]
    ctx = nullcontext() if device_type == 'cpu' else torch.amp.autocast(device_type=device_type, dtype=ptdtype)

    # poor man's data loader
    print(f"Using dataset: {dataset}")
    data_dir = os.path.join('data', dataset)
    print(f"Data directory: {data_dir}")
    def get_batch(split):
        # We recreate np.memmap every batch to avoid a memory leak, as per
        # https://stackoverflow.com/questions/45132940/numpy-memmap-memory-usage-want-to-iterate-once/61472122#61472122
        if split == 'train':
            data = np.memmap(os.path.join(data_dir, 'train.bin'), dtype=np.uint16, mode='r')
        else:
            data = np.memmap(os.path.join(data_dir, 'val.bin'), dtype=np.uint16, mode='r')
        ix = torch.randint(len(data) - block_size, (batch_size,))
        x = torch.stack([torch.from_numpy((data[i:i+block_size]).astype(np.int64)) for i in ix])
        y = torch.stack([torch.from_numpy((data[i+1:i+1+block_size]).astype(np.int64)) for i in ix])
        if device_type == 'cuda':
            # pin arrays x,y, which allows us to move them to GPU asynchronously (non_blocking=True)
            x, y = x.pin_memory().to(device, non_blocking=True), y.pin_memory().to(device, non_blocking=True)
        else:
            x, y = x.to(device), y.to(device)
        return x, y

    # init these up here, can override if init_from='resume' (i.e. from a checkpoint)
    iter_num = 0
    best_val_loss = 1e9

    # attempt to derive vocab_size from the dataset
    meta_path = os.path.join(data_dir, 'meta.pkl')
    meta_vocab_size = None
    if os.path.exists(meta_path):
        with open(meta_path, 'rb') as f:
            meta = pickle.load(f)
        meta_vocab_size = meta['vocab_size']
        print(f"found vocab_size = {meta_vocab_size} (inside {meta_path})")

    # model init
    model_args = dict(n_layer=n_layer, n_head=n_head, n_embd=n_embd, block_size=block_size,
                      bias=bias, vocab_size=None, dropout=dropout) # start with model_args from command line
    if init_from == 'scratch':
        # init a new model from scratch
        print("Initializing a new model from scratch")
        # determine the vocab size we'll use for from-scratch training
        if meta_vocab_size is None:
            print("defaulting to vocab_size of GPT-2 to 50304 (50257 rounded up for efficiency)")
        model_args['vocab_size'] = meta_vocab_size if meta_vocab_size is not None else 50304
        gptconf = GPTConfig(**model_args)
        model = GPT(gptconf)
    elif init_from == 'resume':
        print(f"Resuming training from {out_dir}")
        # resume training from a checkpoint.
        ckpt_path = os.path.join(out_dir, 'ckpt.pt')
        checkpoint = torch.load(ckpt_path, map_location=device)
        checkpoint_model_args = checkpoint['model_args']
        # force these config attributes to be equal otherwise we can't even resume training
        # the rest of the attributes (e.g. dropout) can stay as desired from command line
        for k in ['n_layer', 'n_head', 'n_embd', 'block_size', 'bias', 'vocab_size']:
            model_args[k] = checkpoint_model_args[k]
        # create the model
        gptconf = GPTConfig(**model_args)
        model = GPT(gptconf)
        state_dict = checkpoint['model']
        # fix the keys of the state dictionary :(
        # honestly no idea how checkpoints sometimes get this prefix, have to debug more
        unwanted_prefix = '_orig_mod.'
        for k,v in list(state_dict.items()):
            if k.startswith(unwanted_prefix):
                state_dict[k[len(unwanted_prefix):]] = state_dict.pop(k)
        model.load_state_dict(state_dict)
        iter_num = checkpoint['iter_num']
        best_val_loss = checkpoint['best_val_loss']
    elif init_from.startswith('gpt2'):
        print(f"Initializing from OpenAI GPT-2 weights: {init_from}")
        # initialize from OpenAI GPT-2 weights
        override_args = dict(dropout=dropout)
        model = GPT.from_pretrained(init_from, override_args)
        # read off the created config params, so we can store them into checkpoint correctly
        for k in ['n_layer', 'n_head', 'n_embd', 'block_size', 'bias', 'vocab_size']:
            model_args[k] = getattr(model.config, k)
    # crop down the model block size if desired, using model surgery
    if block_size < model.config.block_size:
        model.crop_block_size(block_size)
        model_args['block_size'] = block_size # so that the checkpoint will have the right value
    model.to(device)

    # initialize a GradScaler. If enabled=False scaler is a no-op
    scaler = torch.cuda.amp.GradScaler(enabled=(dtype == 'float16'))

    # optimizer
    optimizer = model.configure_optimizers(weight_decay, learning_rate, (beta1, beta2), device_type)
    if init_from == 'resume':
        optimizer.load_state_dict(checkpoint['optimizer'])
    checkpoint = None # free up memory

    # compile the model
    if compile:
        print("compiling the model... (takes a ~minute)")
        unoptimized_model = model
        model = torch.compile(model) # requires PyTorch 2.0

    # wrap model into DDP container
    if ddp:
        model = DDP(model, device_ids=[ddp_local_rank])

    # helps estimate an arbitrarily accurate loss over either split using many batches
    @torch.no_grad()
    def estimate_loss():
        out = {}
        model.eval()
        for split in ['train', 'val']:
            losses = torch.zeros(eval_iters)
            for k in range(eval_iters):
                X, Y = get_batch(split)
                with ctx:
                    logits, loss = model(X, Y)
                losses[k] = loss.item()
            out[split] = losses.mean()
        model.train()
        return out

    # learning rate decay scheduler (cosine with warmup)
    def get_lr(it):
        # 1) linear warmup for warmup_iters steps
        if it < warmup_iters:
            return learning_rate * (it + 1) / (warmup_iters + 1)
        # 2) if it > lr_decay_iters, return min learning rate
        if it > lr_decay_iters:
            return min_lr
        # 3) in between, use cosine decay down to min learning rate
        decay_ratio = (it - warmup_iters) / (lr_decay_iters - warmup_iters)
        assert 0 <= decay_ratio <= 1
        coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio)) # coeff ranges 0..1
        return min_lr + coeff * (learning_rate - min_lr)

    # logging
    if wandb_log and master_process:
        import wandb
        wandb.init(project=wandb_project, name=wandb_run_name, config=config_dict)

    # training loop
    X, Y = get_batch('train') # fetch the very first batch
    t0 = time.time()
    local_iter_num = 0 # number of iterations in the lifetime of this process
    raw_model = model.module if ddp else model # unwrap DDP container if needed
    running_mfu = -1.0

    # Setup profiling if enabled
    prof = None
    if enable_profiling:
        prof = torch.profiler.profile(
            activities=[
                torch.profiler.ProfilerActivity.CPU,
                torch.profiler.ProfilerActivity.CUDA,
            ],
            schedule=torch.profiler.schedule(wait=1, warmup=1, active=3, repeat=2),
            on_trace_ready=torch.profiler.tensorboard_trace_handler(f"{TRACE_DIR}/nanogpt_profile"),
            record_shapes=True,
            profile_memory=True,
            with_stack=True
        )
        prof.start()

    while True:

        # determine and set the learning rate for this iteration
        lr = get_lr(iter_num) if decay_lr else learning_rate
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr

        # evaluate the loss on train/val sets and write checkpoints
        if iter_num % eval_interval == 0 and master_process:
            losses = estimate_loss()
            print(f"step {iter_num}: train loss {losses['train']:.4f}, val loss {losses['val']:.4f}")
            if wandb_log:
                wandb.log({
                    "iter": iter_num,
                    "train/loss": losses['train'],
                    "val/loss": losses['val'],
                    "lr": lr,
                    "mfu": running_mfu*100, # convert to percentage
                })
            if losses['val'] < best_val_loss or always_save_checkpoint:
                best_val_loss = losses['val']
                if iter_num > 0:
                    checkpoint = {
                        'model': raw_model.state_dict(),
                        'optimizer': optimizer.state_dict(),
                        'model_args': model_args,
                        'iter_num': iter_num,
                        'best_val_loss': best_val_loss,
                        'config': config_dict,
                    }
                    print(f"saving checkpoint to {out_dir}")
                    torch.save(checkpoint, os.path.join(out_dir, 'ckpt.pt'))
                    checkpoints.commit()
        if iter_num == 0 and eval_only:
            break

        # forward backward update, with optional gradient accumulation to simulate larger batch size
        # and using the GradScaler if data type is float16
        for micro_step in range(gradient_accumulation_steps):
            if ddp:
                # in DDP training we only need to sync gradients at the last micro step.
                # the official way to do this is with model.no_sync() context manager, but
                # I really dislike that this bloats the code and forces us to repeat code
                # looking at the source of that context manager, it just toggles this variable
                model.require_backward_grad_sync = (micro_step == gradient_accumulation_steps - 1)
            with ctx:
                logits, loss = model(X, Y)
                loss = loss / gradient_accumulation_steps # scale the loss to account for gradient accumulation
            # immediately async prefetch next batch while model is doing the forward pass on the GPU
            X, Y = get_batch('train')
            # backward pass, with gradient scaling if training in fp16
            scaler.scale(loss).backward()
        # clip the gradient
        if grad_clip != 0.0:
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
        # step the optimizer and scaler if training in fp16
        scaler.step(optimizer)
        scaler.update()
        # flush the gradients as soon as we can, no need for this memory anymore
        optimizer.zero_grad(set_to_none=True)

        # timing and logging
        t1 = time.time()
        dt = t1 - t0
        t0 = t1
        if iter_num % log_interval == 0 and master_process:
            # get loss as float. note: this is a CPU-GPU sync point
            # scale up to undo the division above, approximating the true total loss (exact would have been a sum)
            lossf = loss.item() * gradient_accumulation_steps
            if local_iter_num >= 5: # let the training loop settle a bit
                mfu = raw_model.estimate_mfu(batch_size * gradient_accumulation_steps, dt)
                running_mfu = mfu if running_mfu == -1.0 else 0.9*running_mfu + 0.1*mfu
            print(f"iter {iter_num}: loss {lossf:.4f}, time {dt*1000:.2f}ms, mfu {running_mfu*100:.2f}%")

        # Step profiler if enabled
        if prof is not None:
            prof.step()

        iter_num += 1
        local_iter_num += 1

        # termination conditions
        if iter_num > max_iters:
            break

    # Stop profiler if enabled and save trace
    if prof is not None:
        prof.stop()
        print(f"Profiling traces saved to {TRACE_DIR}/nanogpt_profile")

    if ddp:
        destroy_process_group()


@app.local_entrypoint()
def main(config_file: str = None, enable_profiling: bool = False):
    """
    Entry point for running modal training with optional config file and profiling.
    Usage: modal run modal_train.py --config-file config/train_shakespeare_char.py --enable-profiling
    """
    train.remote(config_file, enable_profiling)
