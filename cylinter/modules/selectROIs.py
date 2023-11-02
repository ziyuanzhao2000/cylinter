import os
import sys
import yaml
import pickle
import logging
from pathlib import Path
from dataclasses import dataclass, field
from uuid import uuid4

import numpy as np

from tifffile import imread
from PIL import Image, ImageDraw

import matplotlib.pyplot as plt
from matplotlib.backends.qt_compat import QtWidgets
from qtpy.QtCore import QTimer

import napari
from magicgui import magicgui
from magicgui.widgets import ComboBox, SpinBox, Container, Button
from skimage.morphology import flood

from ..utils import (
    input_check, read_markers, get_filepath, marker_channel_number, napari_notification,
    single_channel_pyramid, triangulate_ellipse, reorganize_dfcolumns, 
    upscale, ArtifactInfo, artifact_detector_v3
)

logger = logging.getLogger(__name__)


@dataclass
class GlobalState():
    loaded_ims = {}
    abx_layers = {}
    base_clf = None
    last_sample = None
    artifacts = {}
    _artifact_mask: np.ndarray = field(
        default_factory=lambda: np.array([], dtype=int))
    binarized_artifact_mask: np.ndarray = field(
        default_factory=lambda: np.array([], dtype=bool))
    artifact_proba: np.ndarray = field(
        default_factory=lambda: np.array([], dtype=float))
    artifact_detection_threshold: float = 0.5
    ###
    current_layer: napari.layers.Layer = None
    current_point: np.ndarray = None
    current_tol: int = None

    @property
    def artifact_mask(self):
        return self._artifact_mask

    @artifact_mask.setter
    def artifact_mask(self, data: np.ndarray):
        self._artifact_mask = data
        # may need to remove this hard-coded assumption later
        self.binarized_artifact_mask = self.artifact_mask != 1


def selectROIs(data, self, args):

    global_state = GlobalState()

    check, markers_filepath = input_check(self)

    # read marker metadata
    markers, dna1, dna_moniker, abx_channels = read_markers(
        markers_filepath=markers_filepath,
        markers_to_exclude=self.markersToExclude,
        data=data
    )

    # print(markers_filepath, self.markersToExclude, data)
    # print("########")
    # print(markers)

    if self.samplesForROISelection:

        # check that all samples in samplesForROISelection are in dataframe
        if not (set(self.samplesForROISelection).issubset(data['Sample'].unique())):
            logger.info(
                'Aborting; one or more samples in "samplesForROISelection" configuration '
                'parameter are not in the dataframe.'
            )
            sys.exit()

        # create ROIs directory if it hasn't already
        roi_dir = os.path.join(self.outDir, 'ROIs')
        if not os.path.exists(roi_dir):
            os.makedirs(roi_dir)
        art_dir = os.path.join(roi_dir, self.artifactDetectionMethod)
        if not os.path.exists(art_dir):
            os.makedirs(art_dir)

        samples = iter(self.samplesForROISelection)
        sample = next(samples)

        viewer = napari.Viewer(title='CyLinter')
        viewer.scale_bar.visible = True
        viewer.scale_bar.unit = 'um'

        ###################################################################
        # Load data for the data layer(s), i.e., polygon dicts, and potentially
        # the points corresponding to centroids of cells classified as artifacts.
        def load_extra_layers():
            varname_filename_lst = [('ROI', 'ROI_selection.pkl')]
            layer_type = [('ROI', 'shape')]
            layer_name = [('ROI', 'Negative ROI Selection' if self.delintMode else 'Positive ROI Selection')]
            if self.autoArtifactDetection:
                if self.artifactDetectionMethod == 'MLP':
                    varname_filename_lst += [('ROI2', 'artifact_pred_selection.pkl'),
                                            ('Detected Artifacts', 'points.pkl')]
                    layer_type += [('ROI2', 'shape'), ('Detected Artifacts', 'point')]
                    layer_name += [('ROI2', 'Ignore Artifact Prediction Selection'), 
                                ('Detected Artifacts', 'Predicted Artifacts')]
                elif self.artifactDetectionMethod == 'Classical':
                    # to avoid complication, we implement the most straightforward method
                    # of registering every abx channel, even if some may not have artifacts
                    for abx_channel in abx_channels:
                        varname_filename_lst += [(f'{abx_channel}_mask', f'{abx_channel}_artifact_mask.pkl'),
                                                 (f'{abx_channel}_seeds', f'{abx_channel}_artifact_seeds.pkl')]
                        layer_type += [(f'{abx_channel}_mask', 'image'),
                                       (f'{abx_channel}_seeds', 'point')]
                        layer_name += [(f'{abx_channel}_mask', f'{abx_channel} Artifact Mask'),
                                       (f'{abx_channel}_seeds', f'{abx_channel} Artifact Seeds')]
            layer_type = dict(layer_type)
            layer_name = dict(layer_name)

            extra_layers = {}
            for varname, fname in varname_filename_lst:
                fdir = os.path.join(art_dir, fname)
                print(fdir)
                if os.path.exists(fdir):
                    f = open(fdir, 'rb')
                    extra_layers[varname] = pickle.load(f)
                else:
                    extra_layers[varname] = {}
            return extra_layers, layer_type, layer_name, varname_filename_lst
        
        extra_layers, layer_type, layer_name, varname_filename_lst = load_extra_layers()

        ###################################################################
        def artifact_detection_model_MLP(data):
            if global_state.base_clf is None:
                model_path = os.path.join(Path(__file__).absolute().parent,
                                          '../pretrained_models/pretrained_model.pkl')
                with open(model_path, 'rb') as f:
                    global_state.base_clf = pickle.load(f)

            # Consider put this into the sklearn pipeline?!
            # make sure to strip off cell_id first
            if ('CellID' in data.columns):
                model_input = data.loc[:, data.columns != 'CellID']

            # keep just relevant columns
            marker_list = [
                'Hoechst0', 'anti_CD3', 'anti_CD45RO', 'Keratin_570', 'aSMA_660', 'CD4_488',
                'CD45_PE', 'PD1_647', 'CD20_488', 'CD68_555', 'CD8a_660', 'CD163_488',
                'FOXP3_570', 'PDL1_647', 'Ecad_488', 'Vimentin_555', 'CDX2_647', 'LaminABC_488',
                'Desmin_555', 'CD31_647', 'PCNA_488', 'CollagenIV_647', 'Area', 'MajorAxisLength',
                'MinorAxisLength', 'Eccentricity', 'Solidity', 'Extent', 'Orientation'
            ]

            model_input = model_input[marker_list]

            # clf_probs = base_clf.predict_proba(data.values)
            clf_preds = global_state.base_clf.predict(model_input)
            clf_proba = global_state.base_clf.predict_proba(model_input)
            #####################################################
            return clf_preds, clf_proba

        def save_shapes(sample):
            if self.artifactDetectionMethod == 'MLP':
                for i, tupl in enumerate(varname_filename_lst[::-1]):
                    varname, filename = tupl
                    updated_layer_data = []
                    layer = viewer.layers[-(i + 1)]
                    if layer_type[varname] == 'shape':
                        for shape_type, roi in zip(layer.shape_type, layer.data):
                            updated_layer_data.append((shape_type, roi))
                        extra_layers[varname][sample] = updated_layer_data
                    elif layer_type[varname] == 'point':
                        updated_layer_data = layer.data, global_state.artifact_mask, global_state.artifact_proba
                        extra_layers[varname][sample] = updated_layer_data

                    f = open(os.path.join(art_dir, filename), 'wb')
                    pickle.dump(extra_layers[varname], f)
                    f.close()
            elif self.artifactDetectionMethod == 'Classical':
                pass

        def add_layers(sample):
            # antibody/immunomarker channels
            if self.showAbChannels:
                for ch in reversed(abx_channels):
                    channel_number = marker_channel_number(markers, ch)
                    file_path = get_filepath(self, check, sample, 'TIF')
                    img, min, max = single_channel_pyramid(
                        file_path, channel=channel_number)
                    layer = viewer.add_image(
                        img, rgb=False, blending='additive',
                        colormap='green', visible=False, name=ch,
                        contrast_limits=(min, max)
                    )
                    global_state.loaded_ims[ch] = img
                    global_state.abx_layers[ch] = layer

            # H&E channel, as single image or using separate RGB channels, to be implemented

            # cell segmentation outlines channel
            file_path = get_filepath(self, check, sample, 'SEG')
            seg, min, max = single_channel_pyramid(file_path, channel=0)
            viewer.add_image(
                seg, rgb=False, blending='additive', opacity=0.5,
                colormap='gray', visible=False, name='segmentation'
            )

            # DNA1 channel
            file_path = get_filepath(self, check, sample, 'TIF')
            dna, min, max = single_channel_pyramid(file_path, channel=0)
            viewer.add_image(
                dna, rgb=False, blending='additive', colormap='gray', visible=True,
                name=f'{dna1}: {sample}', contrast_limits=(min, max)
            )

            # ROI selection channel, as well as ROI2 and labeled artifacts
            for varname, layer_data in extra_layers.items():
                if layer_type[varname] == 'shape':
                    try:
                        shapes = [shape_data[0]
                                  for shape_data in layer_data[sample]]
                        polygons = [shape_data[1]
                                    for shape_data in layer_data[sample]]
                    except KeyError:
                        shapes = 'polygon'
                        polygons = None

                    if varname == 'ROI':
                        if self.delintMode:
                            edge_color = [1.0, 0.00, 0.0, 1.0]
                        else:
                            edge_color = [0.0, 0.66, 1.0, 1.0]
                    elif varname == 'ROI2':
                        edge_color = [0.0, 0.66, 1.0, 1.0]

                    viewer.add_shapes(
                        data=polygons, shape_type=shapes, ndim=2,
                        face_color=[1.0, 1.0, 1.0, 0.05], edge_color=edge_color,
                        edge_width=10.0, name=layer_name[varname]
                    )
                elif layer_type[varname] == 'point':
                    if self.autoArtifactDetection:
                        if self.artifactDetectionMethod == 'MLP':
                            try:
                                points, global_state.artifact_mask, global_state.artifact_proba = layer_data[sample]
                                artifact_mask_ = (
                                    global_state.artifact_mask[global_state.binarized_artifact_mask]
                                )
                                artifact_proba_ = (
                                    global_state.artifact_proba[global_state.binarized_artifact_mask]
                                )
                            except:  # may want to be explicit here about the expected exception.
                                points = None
                                artifact_mask_ = []
                                artifact_proba_ = []
                            viewer.add_points(
                                points, ndim=2, edge_color=[0.0, 0.0, 0.0, 0.0],
                                edge_width=0.0, name=layer_name[varname], size=10.0,
                                face_color_cycle={
                                    1: 'white', 2: 'red', 3: 'blue', 4: 'green', 5: 'cyan', 6:
                                    'magenta'
                                }, face_color='artifact_class',
                                features={'artifact_class': np.array(
                                    artifact_mask_, dtype=int)}
                            )  # face_color=[1.0, 0, 0, 0.2],
                            points_layer = viewer.layers[-1]
                            points_layer.face_color_mode = 'direct'
                            points_layer.face_color[:, -1] * \
                                np.array(artifact_proba_)
                            points_layer.refresh()
                        elif self.artifactDetectionMethod == 'Classical':
                            try:
                                artifact_mask, abx_channel = layer_data[sample] #artifact_mask should be upscaled
                                artifact_layer = viewer.add_image(artifact_mask,
                                          name=layer_name[varname], opacity=0.5)
                                artifact_layer.metadata['abx_channel'] = abx_channel
                            except:
                                pass
                elif layer_type[varname] == 'image':
                    if self.autoArtifactDetection and self.artifactDetectionMethod == 'Classical':
                        try:
                            seeds, downscale, tols, abx_channel = layer_data[sample]
                            seed_layer = viewer.add_points(seeds*(2**downscale), 
                                    name=layer_name[varname],
                                    face_color=[1,0,0,1],
                                    edge_color=[0,0,0,0],
                                    size=10, # fix this later to be dynamic!
                                    features={
                                        'id': range(len(seeds)),
                                        'tol': tols
                                    })
                            seed_layer.metadata['abx_channel'] = abx_channel
                            seed_layer.metadata['downscale'] = downscale
                        except:
                            pass

            # Apply previously defined contrast limits if they exist
            if os.path.exists(os.path.join(f'{self.outDir}/contrast/contrast_limits.yml')):

                logger.info('Reading existing contrast settings.')

                contrast_limits = yaml.safe_load(
                    open(f'{self.outDir}/contrast/contrast_limits.yml')
                )

                viewer.layers[
                    f'{dna1}: {sample}'].contrast_limits = (
                    contrast_limits[dna1][0], contrast_limits[dna1][1])

                for ch in reversed(abx_channels):
                    viewer.layers[ch].contrast_limits = (
                        contrast_limits[ch][0], contrast_limits[ch][1])

        def clear_widgets(widgets):
            for widget in widgets:
                viewer.window.remove_dock_widget(widget)

        def add_widgets(last_sample, sample):
            @magicgui(
                call_button="Apply ROI(s) and Move to Next Sample"
            )
            def next_sample(last_sample, sample, widgets):
                try:
                    # update ROI and other shape layers
                    save_shapes(sample)

                    # move to next sample and re-render the GUI
                    if last_sample is None:
                        sample = next(samples)
                    else:
                        sample = last_sample
                        global_state.last_sample = None
                    next_sample.sample.bind(sample)
                    viewer.layers.clear()
                    clear_widgets(widgets)
                    add_layers(sample)
                    add_widgets(last_sample, sample)
                    napari_notification(f'ROI(s) applied to sample {sample}.')
                except StopIteration:
                    print()
                    napari_notification('ROI selection complete')
                    QTimer().singleShot(0, viewer.close)

            next_sample.sample.bind(sample)  # should bind first sample_id
            next_sample.last_sample.bind(global_state.last_sample)

            @magicgui(
                call_button='Enter',
                next_sample={'label': 'Sample Name'}
            )
            def arbitrary_sample(sample, next_sample: str, widgets):
                if global_state.last_sample is None:
                    global_state.last_sample = sample
                save_shapes(sample)
                viewer.layers.clear()
                clear_widgets(widgets)
                add_layers(next_sample)
                add_widgets(global_state.last_sample, next_sample)
            arbitrary_sample.sample.bind(sample)

            @magicgui(
                proba_threshold={'label': 'Threshold',
                                 'widget_type': 'FloatSlider',
                                 'min': 0.001,
                                 'max': 0.999,
                                 'step': 0.001},
                call_button="Auto label artifacts"
            )
            def label_artifacts_MLP(proba_threshold: float = 0.5):
                viewer.layers.selection.active = viewer.layers[-1]
                viewer.layers[-1].data = None
                global_state.artifact_detection_threshold = proba_threshold
                global_state.artifact_mask, class_probas = artifact_detection_model_MLP(data)
                global_state.artifact_proba = 1 - class_probas[:, 0]
                artifact_mask, binarized_artifact_mask, artifact_proba = (
                    global_state.artifact_mask, 
                    global_state.binarized_artifact_mask,
                    global_state.artifact_proba
                )
                centroids = data[['Y_centroid', 'X_centroid']
                                 ][binarized_artifact_mask]
                points_layer = viewer.layers[-1]
                points_layer.face_color_mode = 'cycle'
                points_layer.add(centroids)
                points_layer.features.loc[-len(centroids):, 'artifact_class'] = (
                    artifact_mask[binarized_artifact_mask]
                )
                points_layer.refresh_colors()
                points_layer.face_color_mode = 'direct'
                points_layer.face_color[:, -1] = np.array(
                    artifact_proba[binarized_artifact_mask])
                points_layer.refresh()
                viewer.layers.selection.active = viewer.layers[-2]

            @label_artifacts_MLP.proba_threshold.changed.connect
            def on_slider_changed(threshold):
                # consider using resource management "with...as" here
                viewer.layers.selection.active = viewer.layers[-1]
                points_layer = viewer.layers[-1]
                global_state.artifact_detection_threshold = threshold
                points_layer.face_color_mode = 'direct'
                proba = np.array(
                    global_state.artifact_proba[global_state.binarized_artifact_mask])
                points_layer.face_color[:, -1] = np.piecewise(
                    proba, [proba < threshold, proba > threshold],
                    [lambda v: 0, lambda v: (
                        v - threshold) / (1 - threshold) * (1 - 0.3) + 0.3]
                )
                # np.clip((proba - threshold) / (np.max(proba) - threshold), 0.001, 0.999)
                points_layer.refresh()
                viewer.layers.selection.active = viewer.layers[-2]
            
            # might want to rewrite the above code for MLP model to reduce
            # overhead of defining these widgets if we are never going to use them!
            def generate_widgets_for_classical_method():
                channel_selector_dropdown = ComboBox(choices=abx_channels, label='Abx channel')
                compute_mask_button = Button(label='Compute artifact mask')
                widget_combo_1 = Container(widgets=[
                    channel_selector_dropdown, compute_mask_button
                ])

                tolerance_spinbox = SpinBox(value=0, label='Tolerance')
                widget_combo_2 = Container(widgets=[
                    tolerance_spinbox, 
                ])

                #### bind callbacks
                artifacts = global_state.artifacts
                loaded_ims = global_state.loaded_ims

                @compute_mask_button.clicked.connect
                def compute_artifact_mask():
                    ### first remove any existing layers if exist
                    abx_channel = channel_selector_dropdown.value
                    if abx_channel in artifacts.keys():
                        try:
                            viewer.layers.remove(artifacts[abx_channel].artifact_layer)
                            viewer.layers.remove(artifacts[abx_channel].seed_layer)
                        except:
                            pass
                    ### next, compute
                    params = {'downscale': 2}
                    artifact_mask, im_transformed, seeds, tols = \
                                artifact_detector_v3(loaded_ims[abx_channel],
                                                     downscale=params['downscale'])
                    artifact_info = ArtifactInfo(params, artifact_mask, im_transformed, 
                                                dict(zip(range(len(seeds)), seeds)), tols)
                    artifacts[abx_channel] = artifact_info
                    artifact_info.render(viewer, loaded_ims, layer_name, abx_channel)
                    seed_layer, artifact_layer = artifact_info.seed_layer, artifact_info.artifact_layer
                    
                    def point_clicked_callback(event):
                        current_layer = viewer.layers.selection.active
                        global_state.current_layer = current_layer
                        features = seed_layer.current_properties
                        global_state.current_point = artifact_info.seeds[features['id'][0]]
                        global_state.current_tol = features['tol'][0]
                        tolerance_spinbox.value = features['tol'][0]
                    
                    def point_changed_callback(event):
                        current_layer = viewer.layers.selection.active
                        abx_channel = current_layer.metadata['abx_channel']
                        pt_id = current_layer.current_properties['id'][0]
                        artifact_info = artifacts[abx_channel]
                        im_transformed = artifact_info.transformed
                        global_state.current_layer = current_layer
                        df = artifact_info.seed_layer.features.copy() # preemptive...but might be wasteful if df is large
                        
                        if event.action=='add':
                            df = seed_layer.features
                            pt_id = uuid4()
                            df.loc[df.index[-1], 'id']=pt_id
                            df.loc[df.index[-1], 'tol']=0
                            seed = (current_layer.data[-1] / (2**current_layer.metadata['downscale'])).astype(int)
                            artifact_info.seeds[pt_id] = seed
                            new_fill = flood(im_transformed, seed_point=tuple(seed), 
                                                            tolerance=0)
                            artifact_info.update_mask(artifact_info.mask + new_fill)
                        elif event.action=='remove':
                            optimal_tol = global_state.current_tol
                            old_fill = flood(im_transformed, seed_point=tuple(global_state.current_point), 
                                            tolerance=optimal_tol) 
                            artifact_info.update_mask(artifact_info.mask - old_fill)
                    
                    seed_layer.events.current_properties.connect(point_clicked_callback)
                    seed_layer.events.data.connect(point_changed_callback)

                    for layer in viewer.layers:
                        layer.visible=False
                    artifact_layer.visible=True
                    seed_layer.visible=True
                    global_state.abx_layers[abx_channel].visible=True
                    seed_layer.selected=True

                @tolerance_spinbox.changed.connect
                def update_flood_mask():
                    current_layer = global_state.current_layer
                    abx_channel = current_layer.metadata['abx_channel']
                    artifacts = global_state.artifacts
                    pt_id = current_layer.current_properties['id'][0]
                    df = artifacts[abx_channel].seed_layer.features
                    pt_idx = df[df['id']==pt_id]['tol'].index.to_list()[0]
                    optimal_tol = df.loc[pt_idx, 'tol']
                    im_transformed = artifacts[abx_channel].transformed
                    old_fill = flood(im_transformed, seed_point=tuple(artifacts[abx_channel].seeds[pt_id]), 
                                                    tolerance=optimal_tol) 
                    new_tol = tolerance_spinbox.value
                    new_fill = flood(im_transformed, seed_point=tuple(artifacts[abx_channel].seeds[pt_id]), 
                                                    tolerance=new_tol)
                    artifacts[abx_channel].update_mask(artifacts[abx_channel].mask + new_fill - old_fill)
                    artifacts[abx_channel].seed_layer.features.loc[pt_idx, 'tol'] = new_tol
                ################################################################
                widget_lst = [widget_combo_1, widget_combo_2]
                widget_names = ['Automated artifact detection', 'Finetuning']

                return widget_lst, widget_names

            widgets = [next_sample, arbitrary_sample]
            widget_names = [f'Sample {sample}', 'Arbitrary Sample Selection']
            if self.autoArtifactDetection:
                if self.artifactDetectionMethod == 'MLP':
                    widgets.append(label_artifacts_MLP) # think up better function name?
                    widget_names.append(['Automation Module'])
                elif self.artifactDetectionMethod == 'Classical':
                    new_widget_lst, new_widget_names = generate_widgets_for_classical_method()
                    widgets.extend(new_widget_lst) # need to implement this
                    widget_names.extend(new_widget_names)
            napari_widgets = []
            for widget, widget_name in zip(widgets, widget_names):
                napari_widgets.append(viewer.window.add_dock_widget(widget=widget,
                                                                    add_vertical_stretch=False,
                                                                    name=widget_name))
            next_sample.widgets.bind(napari_widgets)
            arbitrary_sample.widgets.bind(napari_widgets)

            for napari_widget in napari_widgets:
                napari_widget.widget().setSizePolicy(
                    QtWidgets.QSizePolicy.Minimum,
                    QtWidgets.QSizePolicy.Maximum,
                )

        ###################################################################

        add_layers(sample)
        add_widgets(global_state.last_sample, sample)

        napari.run()  # blocks until window is closed

        ###################################################################

        idxs_to_drop = {}
        samples = self.samplesForROISelection
        for sample in samples:
            try:
                have_artifact_layer = (
                    extra_layers['Detected Artifacts'] if self.autoArtifactDetection else False
                )
                if extra_layers['ROI'][sample] or have_artifact_layer:

                    logger.info(f'Generating ROI mask(s) for sample: {sample}')

                    sample_data = data[['X_centroid', 'Y_centroid', 'CellID']][
                        data['Sample'] == sample].astype(int)

                    sample_data['tuple'] = list(
                        zip(sample_data['X_centroid'],
                            sample_data['Y_centroid'])
                    )

                    file_path = get_filepath(self, check, sample, 'TIF')
                    dna, min, max = single_channel_pyramid(
                        file_path, channel=0)

                    # create pillow image to convert into boolean mask
                    def shape_layer_to_mask(layer_data):
                        img = Image.new(
                            'L', (dna[0].shape[1], dna[0].shape[0]))

                        for shape_type, verts in layer_data:

                            selection_verts = np.round(verts).astype(int)

                            if shape_type == 'ellipse':

                                vertices, triangles = triangulate_ellipse(
                                    selection_verts
                                )

                                # flip 2-tuple coordinates returned by
                                # triangulate_ellipse() to draw image mask
                                vertices = [tuple(reversed(tuple(i)))
                                            for i in vertices]

                                # update pillow image with polygon
                                ImageDraw.Draw(img).polygon(
                                    vertices, outline=1, fill=1
                                )

                            else:
                                vertices = list(tuple(
                                    zip(selection_verts[:, 1],
                                        selection_verts[:, 0])
                                ))

                                # update pillow image with polygon
                                ImageDraw.Draw(img).polygon(
                                    vertices, outline=1, fill=1)

                        # convert pillow image into boolean numpy array
                        mask = np.array(img, dtype=bool)
                        return mask

                    # use numpy fancy indexing to get centroids
                    # where boolean mask is True
                    xs, ys = zip(*sample_data['tuple'])
                    ROI_mask = shape_layer_to_mask(extra_layers['ROI'][sample])
                    inter1 = ROI_mask[ys, xs]
                    sample_data['inter1'] = inter1

                    # might want to keep track if auto artifact detection is used separately in the global state
                    if self.artifactDetectionMethod == 'MLP':
                        autoArtifactDetectionUsed = len(global_state.artifact_proba) > 0
                    elif self.artifactDetectionMethod == 'Classical':
                        autoArtifactDetectionUsed = True ## need to fix
                    
                    if self.autoArtifactDetection and autoArtifactDetectionUsed:
                        if self.artifactDetectionMethod == 'MLP':
                            ROI2_mask = shape_layer_to_mask(extra_layers['ROI2'][sample])
                        elif self.artifactDetectionMethod == 'Classical':
                            pass # need to take union of all masks
                        inter2 = ~ROI2_mask[ys, xs] & (global_state.artifact_mask != 1) & \
                            (global_state.artifact_proba > global_state.artifact_detection_threshold)
                        sample_data['inter2'] = inter2

                    drop_artifact_ids = sample_data['inter2'] if self.autoArtifactDetection and autoArtifactDetectionUsed else False
                    if self.delintMode is True:
                        idxs_to_drop[sample] = list(
                            sample_data['CellID'][sample_data['inter1']
                                                  | drop_artifact_ids]
                        )
                    else:
                        idxs_to_drop[sample] = list(
                            sample_data['CellID'][~sample_data['inter1']
                                                  | drop_artifact_ids]
                        )
                else:
                    logger.info(f'No ROIs selected for sample: {sample}')
                    idxs_to_drop[sample] = []

            except KeyError:
                logger.info(
                    f'Aborting; ROIs have not been '
                    f'selected for sample {sample}. '
                    'Please re-run selectROIs module to select '
                    'ROIs for this sample.'
                )
                sys.exit()
        print()

        # drop cells from samples
        for sample, cell_ids in idxs_to_drop.items():
            if cell_ids:
                logger.info(f'Dropping cells from sample: {sample}')
                global_idxs_to_drop = data[
                    (data['Sample'] == sample)
                    & (data['CellID'].isin(set(cell_ids)))].index
                data.drop(global_idxs_to_drop, inplace=True)
            else:
                pass
        print()

        # save plots of selected data points
        plot_dir = os.path.join(roi_dir, 'plots')
        if not os.path.exists(plot_dir):
            os.mkdir(plot_dir)

        for sample in samples:

            logger.info(f'Plotting ROI selections for sample: {sample}')

            file_path = get_filepath(self, check, sample, 'TIF')
            dna, min, max = single_channel_pyramid(file_path, channel=0)

            fig, ax = plt.subplots()
            ax.imshow(dna[0], cmap='gray')
            ax.grid(False)
            ax.set_axis_off()
            coords = data[['X_centroid', 'Y_centroid', 'Area']][
                data['Sample'] == sample]
            ax.scatter(
                coords['X_centroid'], coords['Y_centroid'], s=0.35, lw=0.0, c='yellow'
            )
            plt.title(f'Sample {sample}', size=10)
            plt.tight_layout()
            plt.savefig(os.path.join(plot_dir, f'{sample}.png'), dpi=800)
            plt.close('all')

    else:
        logger.info(
            'Skipping ROI selection, no samples for ROI selection specified in config.yml'
        )

    data = reorganize_dfcolumns(data, markers, self.dimensionEmbedding)

    print()
    print()
    return data