import torch
import torch.nn.functional as F
from tqdm.auto import tqdm
from PIL import Image
import numpy as np
import re
import os
import random
import gc

# 1. Import accelerate
import accelerate

# Set memory allocation strategy
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

# Diffusers and Transformers
from diffusers import AutoencoderKL, UNet2DConditionModel, DDIMScheduler
from transformers import CLIPTextModel, CLIPTokenizer

# Differentiable SVG Renderer
import pydiffvg

import bitmap2svg

# The parse_svg_and_render function remains the same.
def parse_svg_and_render(svg_string: str, width: int, height: int, device: str) -> torch.Tensor:
    # This parser is simplified. For production, a robust XML parser would be better.
    polygons = re.findall(r'<polygon points="([^"]+)" fill="([^"]+)"/>', svg_string)
    shapes, shape_groups = [], []
    for points_str, fill_str in polygons:
        try:
            points_data = [float(p) for p in points_str.replace(',', ' ').split()]
            if not points_data or len(points_data) % 2 != 0: continue
            points = torch.tensor(points_data, dtype=torch.float32, device=device).view(-1, 2)
            hex_color = fill_str.lstrip('#')
            if len(hex_color) == 3: r, g, b = tuple(int(hex_color[i]*2, 16) for i in range(3))
            else: r, g, b = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
            color = torch.tensor([r/255.0, g/255.0, b/255.0, 1.0], device=device)
            path = pydiffvg.Polygon(points=points, is_closed=True)
            shapes.append(path)
            shape_groups.append(pydiffvg.ShapeGroup(shape_ids=torch.tensor([len(shapes) - 1], device=device), fill_color=color))
        except (ValueError, IndexError): continue
    bg_match = re.search(r'<rect .* fill="([^"]+)"/>', svg_string)
    bg_color_tensor = torch.tensor([0.0, 0.0, 0.0], device=device)
    if bg_match:
        hex_color = bg_match.group(1).lstrip('#')
        if len(hex_color) == 3: r, g, b = tuple(int(hex_color[i]*2, 16) for i in range(3))
        else: r, g, b = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
        bg_color_tensor = torch.tensor([r/255.0, g/255.0, b/255.0], device=device)
    if not shapes: return bg_color_tensor.view(1, 3, 1, 1).expand(1, 3, height, width)
    scene_args = pydiffvg.RenderFunction.serialize_scene(width, height, shapes, shape_groups)
    render = pydiffvg.RenderFunction.apply
    img = render(width, height, 2, 2, 0, None, *scene_args)
    img = img[:, :, :3] * img[:, :, 3:4] + (1 - img[:, :, 3:4]) * bg_color_tensor
    img = img.unsqueeze(0).permute(0, 3, 1, 2)
    return img

def generate_svg_with_guidance(
    prompt: str,
    negative_prompt: str = "",
    model_id: str = "runwayml/stable-diffusion-v1-5",
    device: str = "cuda:0", # We will use cuda:0 as the main device
    num_inference_steps: int = 50,
    guidance_scale: float = 7.5,
    vector_guidance_scale: float = 1.5,
    guidance_start_step: int = 5,
    guidance_end_step: int = 40,
    guidance_resolution: int = 256,
    guidance_interval: int = 2,
    output_path: str = "output_guided_svg.svg",
    seed: int | None = None
):
    if seed is None:
        seed = random.randint(0, 2**32 - 1)
    
    generator = torch.Generator(device=device).manual_seed(seed)
    print(f"Using seed: {seed}. Main device: {device}.")

    # --- Load Models with CPU Offloading ---
    print("Loading models with sequential CPU offloading enabled...")
    dtype = torch.float16
    
    # All models are loaded without a device, accelerate will handle it
    vae = AutoencoderKL.from_pretrained(model_id, subfolder="vae", torch_dtype=dtype)
    text_encoder = CLIPTextModel.from_pretrained(model_id, subfolder="text_encoder", torch_dtype=dtype)
    unet = UNet2DConditionModel.from_pretrained(model_id, subfolder="unet", torch_dtype=dtype)
    tokenizer = CLIPTokenizer.from_pretrained(model_id, subfolder="tokenizer")
    scheduler = DDIMScheduler.from_pretrained(model_id, subfolder="scheduler")

    # --- 2. Enable Sequential CPU Offloading ---
    # This will automatically move model layers between the specified GPU and CPU
    # to keep VRAM usage low.
    # We offload all models, but accelerate is smart enough to keep things on the GPU if there is space.
    print("Enabling sequential CPU offloading for all models...")
    unet = accelerate.cpu_offload(unet, execution_device=device)
    vae = accelerate.cpu_offload(vae, execution_device=device)
    text_encoder = accelerate.cpu_offload(text_encoder, execution_device=device)

    try:
        # xFormers can still be used with offloading for attention layers
        unet.enable_xformers_memory_efficient_attention()
        print("xFormers enabled for UNet.")
    except ImportError:
        print("xFormers is not available.")

    # --- Prepare Inputs (code is now simpler) ---
    print("Preparing inputs...")
    height = 512
    width = 512
    
    with torch.no_grad():
        text_input = tokenizer([prompt], padding="max_length", max_length=tokenizer.model_max_length, truncation=True, return_tensors="pt")
        text_embeddings = text_encoder(text_input.input_ids.to(device))[0]
    
        uncond_input = tokenizer([negative_prompt], padding="max_length", max_length=tokenizer.model_max_length, return_tensors="pt")
        uncond_embeddings = text_encoder(uncond_input.input_ids.to(device))[0]
    torch.cuda.empty_cache()

    text_embeddings = torch.cat([uncond_embeddings, text_embeddings]).to(device=device, dtype=dtype)
    
    latent_height = int(height // vae.config.scaling_factor)
    latent_width = int(width // vae.config.scaling_factor)
    latents = torch.randn(
        (1, unet.config.in_channels, latent_height, latent_width),
        generator=generator, device=device, dtype=dtype
    )
    latents = latents * scheduler.init_noise_sigma
    
    print("Starting denoising and guidance loop...")
    scheduler.set_timesteps(num_inference_steps)
    
    svg_params_guidance = {
        'num_colors': 8,
        'simplification_epsilon_factor': 0.015,
        'min_contour_area': (guidance_resolution/512)**2 * 25.0,
        'max_features_to_render': 128
    }

    for i, t in enumerate(tqdm(scheduler.timesteps)):
        # Because of offloading, this UNet call will be slower but won't run out of memory.
        # accelerate handles moving layers to/from the GPU behind the scenes.
        with torch.no_grad():
            latent_model_input = torch.cat([latents] * 2)
            latent_model_input = scheduler.scale_model_input(latent_model_input, t)
            noise_pred = unet(latent_model_input, t, encoder_hidden_states=text_embeddings).sample
        torch.cuda.empty_cache()
        
        noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
        noise_pred_cfg = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)
        
        # --- VECTOR GUIDANCE LOGIC ---
        if guidance_start_step <= i < guidance_end_step and i % guidance_interval == 0:
            with torch.no_grad():
                pred_original_sample = scheduler.step(noise_pred_cfg, t, latents).pred_original_sample
                
                # The VAE call will also use offloading
                decoded_image_tensor = vae.decode(1 / vae.config.scaling_factor * pred_original_sample).sample
                
                if guidance_resolution < height:
                    decoded_image_tensor = F.interpolate(
                        decoded_image_tensor.float(),
                        size=(guidance_resolution, guidance_resolution),
                        mode='bicubic',
                        align_corners=False
                    ).to(dtype)

                img_to_vectorize_scaled = (decoded_image_tensor / 2 + 0.5).clamp(0, 1)
                image_np = (img_to_vectorize_scaled.squeeze(0).permute(1, 2, 0).cpu().float().numpy() * 255).astype(np.uint8)
                pil_image = Image.fromarray(image_np)
                svg_string = bitmap2svg.bitmap_to_svg(pil_image, **svg_params_guidance)
                
                # Rendering still happens on the main device
                rendered_svg_tensor = parse_svg_and_render(svg_string, pil_image.width, pil_image.height, device)
                rendered_svg_tensor_scaled = rendered_svg_tensor * 2.0 - 1.0

                loss = F.mse_loss(decoded_image_tensor, rendered_svg_tensor_scaled)
                
                grad = noise_pred_text - noise_pred_uncond
                noise_pred_cfg = noise_pred_cfg + (grad * loss.item() * vector_guidance_scale)

                del pred_original_sample, decoded_image_tensor, rendered_svg_tensor
            torch.cuda.empty_cache()
        
        latents = scheduler.step(noise_pred_cfg, t, latents).prev_sample
        
        if i % 10 == 0:
            gc.collect()
            torch.cuda.empty_cache()

    # --- Final Image and SVG Generation ---
    print("Generating final image and SVG...")
    with torch.no_grad():
        # Move final latents to the auxiliary GPU for final decoding
        latents_aux = latents.to(device)
        latents_aux = 1 / vae.config.scaling_factor * latents_aux
        image_tensor = vae.decode(latents_aux).sample
    torch.cuda.empty_cache()
    
    # Move final image tensor to CPU for saving
    image_tensor = (image_tensor.to("cpu") / 2 + 0.5).clamp(0, 1)
    image_np = (image_tensor.squeeze(0).permute(1, 2, 0).float().numpy() * 255).astype(np.uint8)
    final_image = Image.fromarray(image_np)
    
    raster_output_path = os.path.splitext(output_path)[0] + ".png"
    final_image.save(raster_output_path)
    print(f"Saved final raster image to: {raster_output_path}")

    final_svg_params = {
        'num_colors': 12,
        'simplification_epsilon_factor': 0.005,
        'min_contour_area': 5.0,
        'max_features_to_render': 0 
    }
    final_svg_string = bitmap2svg.bitmap_to_svg(final_image, **final_svg_params)
    
    with open(output_path, "w") as f:
        f.write(final_svg_string)
    print(f"Saved final SVG to: {output_path}")

    return final_image, final_svg_string

if __name__ == '__main__':

    # 1. The more vector-friendly prompt
    PROMPT = "vector illustration of a gray wool coat with a faux fur collar, flat design, solid colors, minimalist, clean lines"
    NEGATIVE_PROMPT = "photo, realistic, 3d, noisy, texture, blurry, shadow, gradient"

    NUM_STEPS = 50

    # 2. Start with a MUCH lower guidance scale
    VECTOR_GUIDANCE_SCALE = 1.5 

    GUIDANCE_START = int(NUM_STEPS * 0.1) # e.g., step 5
    GUIDANCE_END = int(NUM_STEPS * 0.8)   # e.g., step 40
    
    img, svg = generate_svg_with_guidance(
        prompt=PROMPT,
        negative_prompt=NEGATIVE_PROMPT,
        num_inference_steps=NUM_STEPS,
        guidance_scale=7.5,
        vector_guidance_scale=VECTOR_GUIDANCE_SCALE,
        guidance_start_step=GUIDANCE_START,
        guidance_end_step=GUIDANCE_END,
        output_path="out_optimized.svg",
        seed=42
    )
