import torch
from tensordict import TensorDict
import numpy as np
from verl import DataProto
from verl.utils.dataset.rl_dataset import collate_fn
from verl.utils.model import compute_position_id_with_mask
from agent_system.multi_turn_rollout.prompt import TEXT_AUXILIARY_START, OBS_TEXT_START, OBS_IMAGE_START, PRE_ACTION_START
import verl.utils.torch_functional as verl_F
from transformers import PreTrainedTokenizer
import uuid
import pandas as pd
from verl.models.transformers.qwen2_vl import get_rope_index
from agent_system.multi_turn_rollout.utils import process_image, to_list_of_dict, torch_to_numpy
from agent_system.environments import EnvironmentManagerBase
import math
from typing import List, Tuple, Dict

def preprocess_single_sample(
    item: int,
    config,
    tokenizer: PreTrainedTokenizer,
    gen_batch: DataProto,
    obs: Dict,
    pre_batch_output: DataProto = None,
    processor = None,
    use_action: bool = False,
):
    """
    Process a single observation sample, organizing environment observations (text and/or images) 
    into a format processable by the model.
    
    Parameters:
        item (int): Sample index in the batch
        config: Configuration object containing data processing settings
        tokenizer (PreTrainedTokenizer): Tokenizer for text encoding and decoding
        gen_batch (DataProto): Batch data containing original prompts
        obs (Dict): Environment observation, may contain 'text' and 'image' keys
        pre_batch_output (DataProto, optional): Output from previous batch, used to get previous action
        processor: Image processor for multimodal inputs
        use_action (bool): Whether to use previous action
    
    Returns:
        dict: Contains processed input data such as input_ids, attention_mask, etc.
    """

    raw_prompt = gen_batch.non_tensor_batch['raw_prompt'][item]
    data_source = gen_batch.non_tensor_batch['data_source'][item]
    
    # Get observation components
    obs_texts = obs.get('text', None)
    obs_images = obs.get('image', None)
    obs_text = obs_texts[item] if obs_texts is not None else None
    obs_image = obs_images[item] if obs_images is not None else None
    is_multi_modal = obs_image is not None

    # Get previous action
    pre_action = None
    if pre_batch_output is not None and use_action:
        pre_action = tokenizer.decode(pre_batch_output.batch['responses'][item], skip_special_tokens=True)
    
    # Build chat structure
    obs_content = raw_prompt[0]['content']

    # check if <image> is in the obs_content
    if '<image>' in obs_content:
        # print('\033[93m' + 'Warning: <image> placeholder found in the system prompt. Please make sure to replace it with the actual image data.' + '\033[0m')
        obs_content = obs_content.replace('<image>', '')

    # Build obs content
    if obs_text is not None or pre_action is not None:
        # obs_content += TEXT_AUXILIARY_START
        # Add text observation if exists
        if obs_text is not None:
            # obs_content += OBS_TEXT_START
            obs_content += obs_text
        # Add previous action if exists
        if pre_action is not None:
            obs_content += PRE_ACTION_START
            obs_content += pre_action
    # Add image placeholder if multimodal
    if is_multi_modal:
        obs_content = OBS_IMAGE_START + obs_content
    
    chat = np.array([{
        "content": obs_content,
        "role": "user",
    }])
    
    # Apply chat template
    prompt_with_chat_template = tokenizer.apply_chat_template(
        chat,
        add_generation_prompt=True,
        tokenize=False
    )
    
    # Initialize return dict
    row_dict = {}
    
    # Process multimodal data
    if is_multi_modal:
        # Replace image placeholder with vision tokens
        raw_prompt = prompt_with_chat_template.replace('<image>', '<|vision_start|><|image_pad|><|vision_end|>')
        row_dict['multi_modal_data'] = {'image': [process_image(obs_image)]}
        image_inputs = processor.image_processor(row_dict['multi_modal_data']['image'], return_tensors='pt')
        image_grid_thw = image_inputs['image_grid_thw']
        row_dict['multi_modal_inputs'] = {key: val for key, val in image_inputs.items()}
        if image_grid_thw is not None:
            merge_length = processor.image_processor.merge_size**2
            index = 0
            while '<image>' in prompt_with_chat_template:
                prompt_with_chat_template = prompt_with_chat_template.replace(
                    '<image>',
                    '<|vision_start|>' + '<|placeholder|>' * (image_grid_thw[index].prod() // merge_length) +
                    '<|vision_end|>',
                    1,
                )
                index += 1

            prompt_with_chat_template = prompt_with_chat_template.replace('<|placeholder|>',
                                                                            processor.image_token)

    else:
        raw_prompt = prompt_with_chat_template
    
    input_ids, attention_mask = verl_F.tokenize_and_postprocess_data(prompt=prompt_with_chat_template,
                                                                        tokenizer=tokenizer,
                                                                        max_length=config.data.max_prompt_length,
                                                                        pad_token_id=tokenizer.pad_token_id,
                                                                        left_pad=True,
                                                                        truncation='error')
    
    

    if is_multi_modal:

        position_ids = get_rope_index(
            processor,
            input_ids=input_ids[0],
            image_grid_thw=image_grid_thw,
            attention_mask=attention_mask[0],
        )  # (3, seq_len)
    else:
        position_ids = compute_position_id_with_mask(attention_mask)
    
    # Build final output dict
    row_dict.update({
        'input_ids': input_ids[0],
        'attention_mask': attention_mask[0],
        'position_ids': position_ids[0],
        'raw_prompt_ids': tokenizer.encode(raw_prompt, add_special_tokens=False),
        'index': item,
        'data_source': data_source
    })

    if config.data.get('return_raw_chat', False):
        row_dict['raw_prompt'] = chat.tolist()
    
    return row_dict

def preprocess_batch(
    config,
    gen_batch: DataProto, 
    obs: Dict, 
    pre_batch_output: DataProto = None, 
    tokenizer: PreTrainedTokenizer =None, 
    processor=None
) -> DataProto:
    """
    Process a batch of observation samples, converting environment observations into model-processable format.
    
    Parameters:
        config: Configuration object containing data processing settings
        gen_batch (DataProto): Batch data containing original prompts
        obs (Dict): Environment observation dictionary
            - 'text' (None or List[str]): Text observation data
            - 'image' (np.ndarray or torch.Tensor): Image observation data
        pre_batch_output (DataProto, optional): Output from previous batch
        tokenizer (PreTrainedTokenizer): Tokenizer for text processing
        processor: Processor for multimodal data
    
    Returns:
        DataProto: Contains processed batch data with preserved metadata
    """
    batch_size = len(gen_batch.batch['input_ids'])
    processed_samples = []
    
    # Process each sample in parallel
    for item in range(batch_size):
        # Extract per-sample observations
        processed = preprocess_single_sample(
            item=item,
            config=config,
            tokenizer=tokenizer,
            gen_batch=gen_batch,
            obs=obs,
            pre_batch_output=pre_batch_output,
            processor=processor
        )
        processed_samples.append(processed)
    
    # Aggregate batch data
    batch = collate_fn(processed_samples)
    
    # # Add raw prompts if needed
    # if 'raw_prompt' in processed_samples[0]:
    #     batch['raw_prompt'] = np.array([s['raw_prompt'] for s in processed_samples])
    
    # Create DataProto with preserved metadata
    new_batch = DataProto.from_single_dict(
        data=batch,
        meta_info=gen_batch.meta_info
    )

    return new_batch


def gather_rollout_data(total_batch_list: List[List[Dict]],
                        episode_rewards: np.ndarray,
                        episode_lengths: np.ndarray,
                        success: Dict[str, np.ndarray],
                        traj_uid: np.ndarray,
                        config,
                        ) -> DataProto:
    """
    Collect and organize trajectory data, handling batch size adjustments to meet parallel training requirements.
    
    Parameters:
        total_batch_list (List[List[Dict]): List of trajectory data for each environment
        episode_rewards (np.ndarray): Total rewards for each environment
        episode_lengths (np.ndarray): Total steps for each environment
        success (Dict[str, np.ndarray]): Success samples for each environment
        traj_uid (np.ndarray): Trajectory unique identifiers
        config: Configuration object containing training and batch settings
    
    Returns:
        DataProto: Collected and organized trajectory data
    """
    batch_size = len(total_batch_list)

    episode_rewards_mean = np.mean(episode_rewards)
    episode_rewards_min = np.min(episode_rewards)
    episode_rewards_max = np.max(episode_rewards)

    episode_lengths_mean = np.mean(episode_lengths)
    episode_lengths_min = np.min(episode_lengths)
    episode_lengths_max = np.max(episode_lengths)

    success_rate = {}
    for key, value in success.items():
        success_rate[f"{key}_success_rate"] = np.mean(value)
    
    effective_batch = list()
    for bs in range(batch_size):
        # sum the rewards for each data in total_batch_list[bs]
        for data in total_batch_list[bs]:
            assert traj_uid[bs] == data['traj_uid'], "data is not from the same trajectory"
            if data['active_masks']:
                # episode_rewards
                data['episode_rewards'] = episode_rewards[bs]
                data['episode_rewards_mean'] = episode_rewards_mean
                data['episode_rewards_min'] = episode_rewards_min
                data['episode_rewards_max'] = episode_rewards_max
                # episode_lengths
                data['episode_lengths'] = episode_lengths[bs]
                data['episode_lengths_mean'] = episode_lengths_mean
                data['episode_lengths_min'] = episode_lengths_min
                data['episode_lengths_max'] = episode_lengths_max
                # success_rate
                for key, value in success_rate.items():
                    data[key] = value

                effective_batch.append(data)
    
    def adjust_batch(config, effective_batch):
        size_divisor_ref = config.actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu * config.trainer.n_gpus_per_node
        size_divisor_rollout = config.actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu * config.trainer.n_gpus_per_node
        size_divisor_actor = config.actor_rollout_ref.actor.ppo_mini_batch_size
        size_divisor = np.lcm.reduce(np.array([size_divisor_ref, size_divisor_rollout, size_divisor_actor])).item()

        # check if the batch size is divisible by the dp size, if not, delete the last few samples to make it divisible
        bs = len(effective_batch)
        if bs % size_divisor != 0:
            remainder = bs % size_divisor
            print(f"Current batch size: {bs} cannot be divided by {size_divisor}, randomly deleting {remainder} samples")
            
            keep_size = bs - remainder
            effective_batch = [effective_batch[i] for i in np.random.permutation(bs)[:keep_size]]
        return effective_batch
    
    effective_batch = adjust_batch(config, effective_batch)
        
    # Convert trajectory data to DataProto format
    gen_batch_output = DataProto.from_single_dict(
        data=collate_fn(effective_batch)
    )
    return gen_batch_output

def traj_collect_loop(gen_batch: DataProto, actor_rollout_wg, envs: EnvironmentManagerBase, tokenizer: PreTrainedTokenizer, processor, config) -> DataProto:
    """
    Collects trajectories through parallel agent-environment agent_loop, handling early termination 
    while continuing to interact with other active environments.
    
    Parameters:
        gen_batch (DataProto): Initial batch with prompts to start the agent_loop
        actor_rollout_wg (WorkerGroup): Worker group containing the actor model for policy decisions
        envs (EnvironmentManagerBase): Environment manager containing parallel environment instances
        tokenizer (PreTrainedTokenizer): Tokenizer for text processing
        processor: Processor for multimodal data processing
        config: Configuration dictionary with environment and model settings
    
    Returns:
        DataProto: Trajectory data with sequences, rewards, and other agent_loop information
    """
    # Initialize trajectory collection
    batch_size = len(gen_batch.batch['input_ids'])
    device = gen_batch.batch['input_ids'].device
    
    # Initial observations from the environment
    obs, infos = envs.reset()
    
    batch_output = None
    
    uid = str(uuid.uuid4())
    uid_batch = np.array([uid for _ in range(len(gen_batch.batch))], dtype=object)
    is_done = np.zeros(batch_size, dtype=bool)
    traj_uid = np.array([str(uuid.uuid4()) for _ in range(batch_size)], dtype=object)
    total_batch_list = [list() for _ in range(batch_size)]
    total_infos = [list() for _ in range(batch_size)]
    episode_lengths = np.zeros(batch_size, dtype=np.int32)
    episode_rewards = np.zeros(batch_size, dtype=np.float32)
    # Trajectory collection loop
    for step in range(config.env.max_steps):
        active_masks = np.logical_not(is_done)

        batch = preprocess_batch(config=config,
                                        gen_batch=gen_batch,
                                        obs=obs,
                                        pre_batch_output=batch_output,
                                        tokenizer=tokenizer,
                                        processor=processor)

        if 'multi_modal_inputs' in batch.non_tensor_batch.keys():
            batch_input = batch.pop(
                batch_keys=['input_ids', 'attention_mask', 'position_ids'],
                non_tensor_batch_keys=['raw_prompt_ids', 'multi_modal_data', 'multi_modal_inputs'],
            )
        else:
            batch_input = batch.pop(
                batch_keys=['input_ids', 'attention_mask', 'position_ids'],
                non_tensor_batch_keys=['raw_prompt_ids'],
            )

        batch_input.meta_info = gen_batch.meta_info

        batch_output = actor_rollout_wg.generate_sequences(batch_input)

        batch.non_tensor_batch['uid'] = uid_batch
        batch.non_tensor_batch['traj_uid'] = traj_uid

        batch = batch.union(batch_output)
        
        text_actions = tokenizer.batch_decode(batch.batch['responses'], skip_special_tokens=True)
        
        next_obs, rewards, dones, infos = envs.step(text_actions)

        
        if len(rewards.shape) == 2:
            rewards = rewards.squeeze(1)
        if len(dones.shape) == 2:
            # dones is numpy, delete a dimension
            dones = dones.squeeze(1)

        if 'is_action_valid' in infos[0]:
            batch.non_tensor_batch['is_action_valid'] = np.array([info['is_action_valid'] for info in infos], dtype=bool)
        else:
            batch.non_tensor_batch['is_action_valid'] = np.ones(batch_size, dtype=bool)

        # Create reward tensor, only assign rewards for active environments
        episode_rewards += torch_to_numpy(rewards) * torch_to_numpy(active_masks)
        episode_lengths[active_masks] += 1

        assert len(rewards) == batch_size, f"env should return rewards for all environments, got {len(rewards)} rewards for {batch_size} environments"
        batch.non_tensor_batch['rewards'] = torch_to_numpy(rewards, is_object=True)
        batch.non_tensor_batch['active_masks'] = torch_to_numpy(active_masks, is_object=True)
        
        # Update episode lengths for active environments
        batch_list: list[dict] = to_list_of_dict(batch)

        for i in range(batch_size):
            total_batch_list[i].append(batch_list[i])
            total_infos[i].append(infos[i])

        # Update done states
        is_done = np.logical_or(is_done, dones)
            
        # Update observations for next step
        obs = next_obs

        # Break if all environments are done
        if is_done.all():
            break
    
    success: Dict[str, np.ndarray] = envs.success_evaluator(
                total_infos=total_infos,
                total_batch_list=total_batch_list,
                episode_rewards=episode_rewards, 
                episode_lengths=episode_lengths,
                )

    # Create trajectory data
    gen_batch_output: DataProto = gather_rollout_data(
        total_batch_list=total_batch_list,
        episode_rewards=episode_rewards,
        episode_lengths=episode_lengths,
        success=success,
        traj_uid=traj_uid,
        config=config,
    )
    
    return gen_batch_output