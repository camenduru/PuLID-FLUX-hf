import time
import os

import gradio as gr
import torch
from einops import rearrange
from PIL import Image

from flux.cli import SamplingOptions
from flux.sampling import denoise, get_noise, get_schedule, prepare, unpack
from flux.util import load_ae, load_clip, load_flow_model, load_t5
from pulid.pipeline_flux import PuLIDPipeline
from pulid.utils import resize_numpy_image_long


def get_models(name: str, device: torch.device, offload: bool):
    t5 = load_t5(device, max_length=128)
    clip = load_clip(device)
    model = load_flow_model(name, device="cpu" if offload else device)
    model.eval()
    ae = load_ae(name, device="cpu" if offload else device)
    return model, ae, t5, clip


class FluxGenerator:
    def __init__(self):
        self.device = torch.device('cuda')
        self.offload = False
        self.model_name = 'flux-dev'
        self.model, self.ae, self.t5, self.clip = get_models(
            self.model_name,
            device=self.device,
            offload=self.offload,
        )
        self.pulid_model = PuLIDPipeline(self.model, 'cuda', weight_dtype=torch.bfloat16)
        self.pulid_model.load_pretrain()


flux_generator = FluxGenerator()

@torch.inference_mode()
def generate_image(
        width,
        height,
        num_steps,
        start_step,
        guidance,
        seed,
        prompt,
        id_image=None,
        id_weight=1.0,
        neg_prompt="",
        true_cfg=1.0,
        timestep_to_start_cfg=1,
        max_sequence_length=128,
):
    flux_generator.t5.max_length = max_sequence_length

    seed = int(seed)
    if seed == -1:
        seed = None

    opts = SamplingOptions(
        prompt=prompt,
        width=width,
        height=height,
        num_steps=num_steps,
        guidance=guidance,
        seed=seed,
    )

    if opts.seed is None:
        opts.seed = torch.Generator(device="cpu").seed()
    print(f"Generating '{opts.prompt}' with seed {opts.seed}")
    t0 = time.perf_counter()

    use_true_cfg = abs(true_cfg - 1.0) > 1e-2

    if id_image is not None:
        id_image = resize_numpy_image_long(id_image, 1024)
        id_embeddings, uncond_id_embeddings = flux_generator.pulid_model.get_id_embedding(id_image, cal_uncond=use_true_cfg)
    else:
        id_embeddings = None
        uncond_id_embeddings = None

    print(id_embeddings)

    # prepare input
    x = get_noise(
        1,
        opts.height,
        opts.width,
        device=flux_generator.device,
        dtype=torch.bfloat16,
        seed=opts.seed,
    )
    print(x)
    timesteps = get_schedule(
        opts.num_steps,
        x.shape[-1] * x.shape[-2] // 4,
        shift=True,
    )

    if flux_generator.offload:
        flux_generator.t5, flux_generator.clip = flux_generator.t5.to(flux_generator.device), flux_generator.clip.to(flux_generator.device)
    inp = prepare(t5=flux_generator.t5, clip=flux_generator.clip, img=x, prompt=opts.prompt)
    inp_neg = prepare(t5=flux_generator.t5, clip=flux_generator.clip, img=x, prompt=neg_prompt) if use_true_cfg else None

    # offload TEs to CPU, load model to gpu
    if flux_generator.offload:
        flux_generator.t5, flux_generator.clip = flux_generator.t5.cpu(), flux_generator.clip.cpu()
        torch.cuda.empty_cache()
        flux_generator.model = flux_generator.model.to(flux_generator.device)

    # denoise initial noise
    x = denoise(
        flux_generator.model, **inp, timesteps=timesteps, guidance=opts.guidance, id=id_embeddings, id_weight=id_weight,
        start_step=start_step, uncond_id=uncond_id_embeddings, true_cfg=true_cfg,
        timestep_to_start_cfg=timestep_to_start_cfg,
        neg_txt=inp_neg["txt"] if use_true_cfg else None,
        neg_txt_ids=inp_neg["txt_ids"] if use_true_cfg else None,
        neg_vec=inp_neg["vec"] if use_true_cfg else None,
    )

    # offload model, load autoencoder to gpu
    if flux_generator.offload:
        flux_generator.model.cpu()
        torch.cuda.empty_cache()
        flux_generator.ae.decoder.to(x.device)

    # decode latents to pixel space
    x = unpack(x.float(), opts.height, opts.width)
    with torch.autocast(device_type=flux_generator.device.type, dtype=torch.bfloat16):
        x = flux_generator.ae.decode(x)

    if flux_generator.offload:
        flux_generator.ae.decoder.cpu()
        torch.cuda.empty_cache()

    t1 = time.perf_counter()

    print(f"Done in {t1 - t0:.1f}s.")
    # bring into PIL format
    x = x.clamp(-1, 1)
    # x = embed_watermark(x.float())
    x = rearrange(x[0], "c h w -> h w c")

    img = Image.fromarray((127.5 * (x + 1.0)).cpu().byte().numpy())
    return img, str(opts.seed), flux_generator.pulid_model.debug_img_list

_HEADER_ = '''
<div style="text-align: center; max-width: 650px; margin: 0 auto;">
    <h1 style="font-size: 2.5rem; font-weight: 700; margin-bottom: 1rem; display: contents;">PuLID for FLUX</h1>
    <p style="font-size: 1rem; margin-bottom: 1.5rem;">Paper: <a href='https://arxiv.org/abs/2404.16022' target='_blank'>PuLID: Pure and Lightning ID Customization via Contrastive Alignment</a> | Codes: <a href='https://github.com/ToTheBeginning/PuLID' target='_blank'>GitHub</a></p>
</div>

❗️❗️❗️**Tips:**
- `timestep to start inserting ID:` The smaller the value, the higher the fidelity, but the lower the editability; the higher the value, the lower the fidelity, but the higher the editability. **The recommended range for this value is between 0 and 4**. For photorealistic scenes, we recommend using 4; for stylized scenes, we recommend using 0-1. If you are not satisfied with the similarity, you can lower this value; conversely, if you are not satisfied with the editability, you can increase this value.
- `true CFG scale:` In most scenarios, it is recommended to use a fake CFG, i.e., setting the true CFG scale to 1, and just adjusting the guidance scale. This is also more efficiency. However, in a few cases, utilizing a true CFG can yield better results. For more detaileds, please refer to the [doc](https://github.com/ToTheBeginning/PuLID/blob/main/docs/pulid_for_flux.md#useful-tips).
- please refer to the <a href='https://github.com/ToTheBeginning/PuLID/blob/main/docs/pulid_for_flux.md' target='_blank'>github doc</a> for more details and info about the model, we provide the detail explanation about the above two parameters in the doc.
- we provide some examples in the bottom, you can try these example prompts first
'''  # noqa E501

_CITE_ = r"""
If PuLID is helpful, please help to ⭐ the <a href='https://github.com/ToTheBeginning/PuLID' target='_blank'> Github Repo</a>. Thanks!
---

📧 **Contact**
If you have any questions or feedbacks, feel free to open a discussion or contact <b>wuyanze123@gmail.com</b>.
"""  # noqa E501


def create_demo(args, model_name: str, device: str = "cuda" if torch.cuda.is_available() else "cpu",
                offload: bool = False):
    with gr.Blocks() as demo:
        gr.Markdown(_HEADER_)

        with gr.Row():
            with gr.Column():
                prompt = gr.Textbox(label="Prompt", value="portrait, color, cinematic")
                id_image = gr.Image(label="ID Image")
                id_weight = gr.Slider(0.0, 3.0, 1, step=0.05, label="id weight")

                width = gr.Slider(256, 1536, 896, step=16, label="Width")
                height = gr.Slider(256, 1536, 1152, step=16, label="Height")
                num_steps = gr.Slider(1, 20, 20, step=1, label="Number of steps")
                start_step = gr.Slider(0, 10, 0, step=1, label="timestep to start inserting ID")
                guidance = gr.Slider(1.0, 10.0, 4, step=0.1, label="Guidance")
                seed = gr.Textbox(-1, label="Seed (-1 for random)")
                max_sequence_length = gr.Slider(128, 512, 128, step=128,
                                                label="max_sequence_length for prompt (T5), small will be faster")

                with gr.Accordion("Advanced Options (True CFG, true_cfg_scale=1 means use fake CFG, >1 means use true CFG, if using true CFG, we recommend set the guidance scale to 1)", open=False):    # noqa E501
                    neg_prompt = gr.Textbox(
                        label="Negative Prompt",
                        value="bad quality, worst quality, text, signature, watermark, extra limbs")
                    true_cfg = gr.Slider(1.0, 10.0, 1, step=0.1, label="true CFG scale")
                    timestep_to_start_cfg = gr.Slider(0, 20, 1, step=1, label="timestep to start cfg", visible=args.dev)

                generate_btn = gr.Button("Generate")

            with gr.Column():
                output_image = gr.Image(label="Generated Image")
                seed_output = gr.Textbox(label="Used Seed")
                intermediate_output = gr.Gallery(label='Output', elem_id="gallery", visible=args.dev)
                gr.Markdown(_CITE_)

        with gr.Row(), gr.Column():
                gr.Markdown("## Examples")
                example_inps = [
                    [
                        'a woman holding sign with glowing green text \"PuLID for FLUX\"',
                        'example_inputs/liuyifei.png',
                        4, 4, 2680261499100305976, 1
                    ],
                    [
                        'portrait, side view',
                        'example_inputs/liuyifei.png',
                        4, 4, 1205240166692517553, 1
                    ],
                    [
                        'white-haired woman with vr technology atmosphere, revolutionary exceptional magnum with remarkable details',  # noqa E501
                        'example_inputs/liuyifei.png',
                        4, 4, 6349424134217931066, 1
                    ],
                    [
                        'a young child is eating Icecream',
                        'example_inputs/liuyifei.png',
                        4, 4, 10606046113565776207, 1
                    ],
                    [
                        'a man is holding a sign with text \"PuLID for FLUX\", winter, snowing, top of the mountain',
                        'example_inputs/pengwei.jpg',
                        4, 4, 2410129802683836089, 1
                    ],
                    [
                        'portrait, candle light',
                        'example_inputs/pengwei.jpg',
                        4, 4, 17522759474323955700, 1
                    ],
                    [
                        'profile shot dark photo of a 25-year-old male with smoke escaping from his mouth, the backlit smoke gives the image an ephemeral quality, natural face, natural eyebrows, natural skin texture, award winning photo, highly detailed face, atmospheric lighting, film grain, monochrome',  # noqa E501
                        'example_inputs/pengwei.jpg',
                        4, 4, 17733156847328193625, 1
                    ],
                    [
                        'American Comics, 1boy',
                        'example_inputs/pengwei.jpg',
                        1, 4, 13223174453874179686, 1
                    ],
                    [
                        'portrait, pixar',
                        'example_inputs/pengwei.jpg',
                        1, 4, 9445036702517583939, 1
                    ],
                ]
                gr.Examples(examples=example_inps, inputs=[prompt, id_image, start_step, guidance, seed, true_cfg],
                            label='fake CFG')

                example_inps = [
                    [
                        'portrait, made of ice sculpture',
                        'example_inputs/lecun.jpg',
                        1, 1, 3811899118709451814, 5
                    ],
                ]
                gr.Examples(examples=example_inps, inputs=[prompt, id_image, start_step, guidance, seed, true_cfg],
                            label='true CFG')

        generate_btn.click(
            fn=generate_image,
            inputs=[width, height, num_steps, start_step, guidance, seed, prompt, id_image, id_weight, neg_prompt,
                    true_cfg, timestep_to_start_cfg, max_sequence_length],
            outputs=[output_image, seed_output, intermediate_output],
        )

    return demo


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="PuLID for FLUX.1-dev")
    parser.add_argument("--name", type=str, default="flux-dev", choices=list('flux-dev'),
                        help="currently only support flux-dev")
    parser.add_argument("--device", type=str, default="cuda" if torch.cuda.is_available() else "cpu",
                        help="Device to use")
    parser.add_argument("--offload", action="store_true", help="Offload model to CPU when not in use")
    parser.add_argument("--port", type=int, default=7860, help="Port to use")
    parser.add_argument("--dev", action='store_true', help="Development mode")
    parser.add_argument("--pretrained_model", type=str, help='for development')
    args = parser.parse_args()

    demo = create_demo(args, args.name, args.device, args.offload)
    demo.launch(share=True, server_name='0.0.0.0')
